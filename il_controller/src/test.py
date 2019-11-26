import sys

from collections import OrderedDict

sys.path.append('./Data_processing/')
sys.path.append('./')

import math

import matplotlib

matplotlib.use('Agg')
from data_monitor import *
from train import forward_pass, forward_pass_jit, load_settings_from_model
from dataset import set_encoders
from Components.mdn import sample_mdn, sample_mdn_ml
from policy_value_network import PolicyValueNet
import numpy as np

from std_msgs.msg import Float32, Int32


def set_decoders():
    if config.head_mode == "mdn":
        decode_steer_degree = MdnSteerDecoderNormalized2Degree()  # conversion from id to normalized steering
        decode_acc_raw = MdnAccDecoderNormalized2Raw()  # conversion from id to normalized acceleration
        decode_vel = MdnVelDecoderNormalized2Raw()  # conversion from id to normalized command velocity
    elif config.head_mode == "hybrid":
        decode_steer_degree = SteerDecoderOnehot2Degree()  # one-hot vector of steering
        decode_acc_raw = MdnAccDecoderNormalized2Raw()  # conversion from id to normalized acceleration
        decode_vel = MdnVelDecoderNormalized2Raw()  # conversion from id to normalized command velocity
    else:
        decode_steer_degree = SteerDecoderOnehot2Degree()  # one-hot vector of steering
        decode_acc_raw = AccDecoderOnehot2Raw()  # one-hot vector of acceleration
        decode_vel = VelDecoderOnehot2Raw()  # one-hot vector of command velocity
    decode_lane = LaneDecoderOnehot2Int()  # one-hot vector of command velocity

    return decode_steer_degree, decode_acc_raw, decode_vel, decode_lane


def get_copy(t):
    if t is not None:
        return t.clone()
    else:
        return None


def print_full(msg, tensor):
    print(msg)
    for i in range(tensor.size()[0]):
        for j in range(tensor.size()[1]):
            value = float(tensor[i][j].cpu())
            print(value, end=',')
        print()


class DriveController(nn.Module):
    def __init__(self, net):
        super(DriveController, self).__init__()
        clear_png_files('./visualize/', remove_flag='test_')
        self.data_monitor = DataMonitor()
        self.drive_net = net
        # self.cmd_pub = rospy.Publisher('cmd_vel_drive_net', Twist, queue_size=1)
        self.cmd_acc_pub = rospy.Publisher('imitation_cmd_acc', Float32, queue_size=1)
        self.cmd_steer_pub = rospy.Publisher('imitation_cmd_steer', Float32, queue_size=1)
        self.cmd_lane_pub = rospy.Publisher('imitation_lane_decision', Int32, queue_size=1)

        rospy.Subscriber("odom", Odometry, self.odom_call_back)

        self.drive_timer = rospy.Timer(rospy.Duration(1.0 / config.control_freq), self.control_loop)

        self.cur_vel = None
        self.car_info = None
        self.sm = nn.Softmax(dim=1)

        self.encode_input = InputEncoder()
        self.encode_steer_from_degree, self.encode_acc_from_id, self.encode_vel_from_raw, self.encode_lane_from_int = \
            set_encoders()
        self.decode_steer_to_degree, self.decode_acc_to_raw, self.decode_vel, self.decode_lane = set_decoders()

        self.count = 0
        self.true_steering = 0
        self.update_steering = True
        self.dummy_count = 0

        self.label_ts = None

        self.acc_iter = 0
        self.old_acceleration = 0
        self.inference_count = 0

        # for visualization
        self.input_record = OrderedDict()
        self.output_record = OrderedDict()

    def vel_call_back(self, data):
        self.label_ts = time.time()
        self.cur_vel = data.linear.y
        print('Update current vel %f' % self.cur_vel)

    def odom_call_back(self, odo):
        self.cur_vel = odo.twist.twist.linear.x
        # print('Update current vel %f from odometry' % self.cur_vel)

    def cb_car_info(self, car_info):
        self.car_info = car_info

    def control_loop(self, time_step):
        if self.car_info is None:
            print("ego_car not exist yet...")
            return

        data_monitor_alive = self.data_monitor.check_alive()

        if not data_monitor_alive:

            if config.draw_prediction_records:
                self.visualize_hybrid_record()

            print("Node shutting down: data supply is broken")
            rospy.signal_shutdown("Data supply is broken")

        do_loop = self.check_do_loop()
        if not do_loop:
            print('skipping loop')
            return False

        if self.data_monitor.steering_is_triggered and self.data_monitor.steering_topic_alive:
            self.data_monitor.steering_topic_alive = False

        self.update_steering = False
        # self.data_monitor.update_steering = False

        self.data_monitor.update_data = False

        print('Disable data update')
        self.data_monitor.lock.acquire()
        start_time = time.time()

        try:
            if not self.data_monitor.data_valid():  # wait for valid data
                self.update_steering = True
                self.data_monitor.update_data = True
                self.data_monitor.update_steering = True
                print('Data not valid, skipping inference')
                return False

            if self.data_monitor.test_terminal():  # stop the car after reaching goal
                self.publish_terminal_cmd()
                print('Goal reached, skipping inference')
                return True

            acc_label, ang_label, vel_label, lane_label = self.get_labels_combined()

            print("start inference: counter: " + str(self.count))

            # query the drive_net using current data
            if config.head_mode == "mdn":
                # Forward pass
                acc_pi, acc_mu, acc_sigma, \
                ang_pi, ang_mu, ang_sigma, \
                vel_pi, vel_mu, vel_sigma, lane_logits, value = self.inference()

                self.update_steering = True

                lane_probs = self.sm(lane_logits)

                acceleration, steering, velocity, lane = self.sample_from_mdn_distribution(acc_pi, acc_mu, acc_sigma,
                                                                                           ang_pi, ang_mu, ang_sigma,
                                                                                           vel_pi, vel_mu, vel_sigma,
                                                                                           lane_probs)

                self.visualize_mdn_predictions(acc_pi, acc_mu, acc_sigma,
                                               ang_pi, ang_mu, ang_sigma,
                                               vel_pi, vel_mu, vel_sigma,
                                               lane_probs,
                                               acc_label, ang_label, vel_label, lane_label)

            elif config.head_mode == "hybrid":
                # Forward pass
                acc_pi, acc_mu, acc_sigma, \
                ang_logits, \
                vel_pi, vel_mu, vel_sigma, lane_logits, value = self.inference()

                # print("================predicted value:", value)

                self.update_steering = True

                # print("re-open steering update")

                ang_probs = self.sm(ang_logits)
                lane_probs = self.sm(lane_logits)

                acceleration, steering, velocity, lane = self.sample_from_hybrid_distribution(acc_pi, acc_mu, acc_sigma,
                                                                                              ang_probs,
                                                                                              vel_pi, vel_mu, vel_sigma,
                                                                                              lane_probs)

                self.visualize_hybrid_predictions(acc_pi, acc_mu, acc_sigma,
                                                  ang_probs,
                                                  vel_pi, vel_mu, vel_sigma,
                                                  lane_probs,
                                                  value,
                                                  acc_label, ang_label, vel_label, lane_label,
                                                  None,
                                                  acceleration)

            else:
                # Forward pass
                acc_logits, ang_logits, vel_logits, lane_logits, value = self.inference()

                self.update_steering = True

                acc_probs, ang_probs, vel_probs, lane_probs = self.get_sm_probs(acc_logits, ang_logits, vel_logits,
                                                                                lane_logits)

                self.visualize_predictions(acc_probs, ang_probs, vel_probs, lane_probs, acc_label,
                                           ang_label, vel_label, lane_label)

                acceleration, steering, velocity, lane = \
                    self.sample_from_categorical_distribution(acc_probs, ang_probs, vel_probs, lane_probs)

            self.count += 1

            # construct ros topics for the outputs
            print("Steering bin: %d" % steering)
            steering = self.decode_steer_to_degree(steering)
            acceleration = self.decode_acc_to_raw(acceleration)
            if config.use_vel_head:
                velocity = self.decode_vel(velocity)
            lane = self.decode_lane(lane)

            true_steering_degree = self.decode_steer_to_degree(ang_label)
            true_acceleration = self.decode_acc_to_raw(acc_label)
            if config.use_vel_head:
                true_velocity = self.decode_vel(vel_label)
            true_lane = self.decode_lane(lane_label)

            if self.acc_iter == config.acc_slow_down:
                self.old_acceleration = acceleration
                self.acc_iter = 0
            else:
                self.acc_iter += 1
                acceleration = self.old_acceleration

            self.publish_actions(acceleration, steering, velocity, lane,
                                 true_acceleration, true_steering_degree, true_velocity, true_lane)

            self.data_monitor.update_data = True

            elapsed_time = time.time() - start_time
            print("Elapsed time in controlloop: %fs" % elapsed_time)

            self.data_monitor.update_steering = True

            return True
        finally:
            self.release_all_locks()
            return False

    def release_all_locks(self):
        if self.data_monitor.lock.locked():
            self.data_monitor.lock.release()
        self.update_steering = True
        self.data_monitor.update_steering = True
        self.data_monitor.update_data = True

    def cal_pub_acc(self, acceleration):
        target_vel = self.cur_vel + acceleration / config.control_freq
        target_vel = max(min(target_vel, config.vel_max), 0.0)  # target_speed_

        throttle = (target_vel - self.cur_vel + 0.05) * 1.0
        throttle = min(0.5, throttle)
        throttle = max(-0.01, throttle)

        if self.cur_vel <= 0.05 and throttle < 0:
            throttle = 0.0
        return throttle

    def cal_pub_steer(self, steering_degree):
        return steering_degree / self.car_info.max_steer_angle

    def publish_actions(self, acceleration, steering_degree, velocity, lane,
                        true_steering_degree, true_accelaration, true_vel, true_lane):
        try:
            cmd_acc = Float32()
            cmd_steer = Float32()
            cmd_lane = Int32()

            publish_true_steering = False
            if publish_true_steering:
                print('Publishing ground-truth angle')
                cmd_steer.data = self.cal_pub_steer(float(true_steering_degree))
                publish_true_steering = bool(math.fabs(steering_degree - np.degrees(true_steering_degree)) > 20)
            else:
                print('Publishing predicted angle')
                cmd_steer.data = self.cal_pub_steer(steering_degree)

            cmd_acc.data = self.cal_pub_acc(acceleration)  # _
            cmd_lane.data = lane

            if config.fit_ang or config.fit_action or config.fit_all:
                print("output angle in degrees: %f" % float(steering_degree))
                print("ground-truth angle: " + str(true_steering_degree))
            if config.fit_acc or config.fit_action or config.fit_all:
                print("output acc: %f" % float(acceleration))
                print("ground-truth acc: " + str(true_accelaration))
            if (config.fit_vel or config.fit_action or config.fit_all) and config.use_vel_head:
                print("output vel: %f" % float(velocity))
                print("ground-truth angle: " + str(true_vel))
            if config.fit_lane or config.fit_action or config.fit_all:
                print("output lane: %f" % float(lane))
                print("ground-truth lane: " + str(true_lane))

            # publish action and acc commands
            self.cmd_acc_pub.publish(cmd_acc)
            self.cmd_steer_pub.publish(cmd_steer)
            self.cmd_lane_pub.publish(cmd_lane)
        except Exception as e:
            print("Exception when publishing commands: %s", e)
            error_handler(e)

    @staticmethod
    def sample_categorical(probs):
        distrib = Categorical(probs=probs)
        bin = distrib.sample()
        return bin

    @staticmethod
    def sample_categorical_ml(probs):
        # print('probs: ', probs)
        values, indices = probs.max(1)
        # print('indices: ', indices)
        bin = indices[0]
        return bin

    def sample_from_categorical_distribution(self, acc_probs, ang_probs, vel_probs, lane_probs):
        steering_bin = self.sample_categorical_ml(probs=ang_probs)
        acceleration_bin = self.sample_categorical(probs=acc_probs)
        velocity_bin = None
        if config.use_vel_head:
            velocity_bin = self.sample_categorical(probs=vel_probs)
        lane_bin = self.sample_categorical(probs=lane_probs)

        return acceleration_bin, steering_bin, velocity_bin, lane_bin

    @staticmethod
    def sample_guassian_mixture(pi, mu, sigma, mode="ml", component="acc"):
        # print('mdn mu params:', mu)

        if mode == 'ml':
            return float(sample_mdn_ml(pi, sigma, mu, component))
        else:
            return float(sample_mdn(pi, sigma, mu))

    def sample_from_mdn_distribution(self, acc_pi, acc_mu, acc_sigma,
                                     ang_pi, ang_mu, ang_sigma,
                                     vel_pi, vel_mu, vel_sigma, lane_probs):
        steering = self.sample_guassian_mixture(ang_pi, ang_mu, ang_sigma, mode="ml", component="steer")
        acceleration = self.sample_guassian_mixture(acc_pi, acc_mu, acc_sigma, mode="ml", component="acc")
        velocity = None
        if config.use_vel_head:
            velocity = self.sample_guassian_mixture(vel_pi, vel_mu, vel_sigma)
        lane_bin = self.sample_categorical(probs=lane_probs)

        return acceleration, steering, velocity, lane_bin

    def sample_from_hybrid_distribution(self, acc_pi, acc_mu, acc_sigma,
                                        ang_probs,
                                        vel_pi, vel_mu, vel_sigma, lane_probs):
        # steering_bin = self.sample_categorical(probs=ang_probs)
        steering_bin = self.sample_categorical(probs=ang_probs)

        # sample_mode = 'default'
        # if np.random.uniform(0.0, 1.0) > max(1.0 - float(self.count)/(20.0*config.control_freq), 0.1):
        #     sample_mode = 'ml'
        sample_mode = 'ml'
        acceleration = self.sample_guassian_mixture(acc_pi, acc_mu, acc_sigma, sample_mode)

        velocity = None
        if config.use_vel_head:
            velocity = self.sample_guassian_mixture(vel_pi, vel_mu, vel_sigma)
        lane_bin = self.sample_categorical(probs=lane_probs)

        return acceleration, steering_bin, velocity, lane_bin

    def visualize_predictions(self, acc_probs, ang_probs, vel_probs, lane_probs,
                              acc_label, steering_label, vel_label, lane_label):
        if config.visualize_inter_data:
            start_time = time.time()
            encoded_acc_label, encoded_ang_label, encoded_vel_label, encoded_lane_label = \
                self.get_encoded_labels(acc_label, steering_label, vel_label, lane_label)

            try:
                visualize_output_with_labels('test/' + str(self.count), acc_probs, ang_probs, vel_probs, lane_probs,
                                             encoded_acc_label, encoded_ang_label,
                                             encoded_vel_label, encoded_lane_label)
            except Exception as e:
                print("Exception when visualizing angles:", e)
                error_handler(e)

            elapsed_time = time.time() - start_time
            print("Visualization time: " + str(elapsed_time) + " s")

    def visualize_mdn_predictions(self, acc_pi, acc_mu, acc_sigma,
                                  ang_pi, ang_mu, ang_sigma,
                                  vel_pi, vel_mu, vel_sigma,
                                  lane_probs,
                                  acc_label, steering_label, vel_label, lane_label):
        if config.visualize_inter_data:
            start_time = time.time()
            encoded_acc_label, encoded_ang_label, encoded_vel_label, encoded_lane_label = self.get_encoded_mdn_labels(
                acc_label, steering_label, vel_label, lane_label)

            try:
                visualize_mdn_output_with_labels('test/' + str(self.count), acc_mu, acc_pi, acc_sigma, ang_mu, ang_pi,
                                                 ang_sigma, vel_mu, vel_pi, vel_sigma, lane_probs,
                                                 encoded_acc_label, encoded_ang_label,
                                                 encoded_vel_label, encoded_lane_label)

            except Exception as e:
                print("Exception when visualizing angles:", e)
                error_handler(e)

            elapsed_time = time.time() - start_time
            print("Visualization time: " + str(elapsed_time) + " s")

    def visualize_hybrid_predictions(self, acc_pi, acc_mu, acc_sigma, ang_probs,
                                     vel_pi, vel_mu, vel_sigma, lane_probs, value,
                                     acc_label, steering_label, vel_label, lane_label,
                                     v_label, acceleration, draw_truth=True, show_axis=True):

        encoded_acc_label, encoded_ang_label, encoded_vel_label, encoded_lane_label = self.get_encoded_hybrid_labels(
            acc_label, steering_label, vel_label, lane_label)
        if config.visualize_inter_data:
            start_time = time.time()
            try:
                visualize_hybrid_output_with_labels('test/' + str(self.count), acc_mu, acc_pi, acc_sigma, ang_probs,
                                                    vel_mu, vel_pi, vel_sigma, lane_probs, value,
                                                    encoded_acc_label, encoded_ang_label, encoded_vel_label,
                                                    encoded_lane_label, v_label,
                                                    acceleration, draw_truth, show_axis)

            except Exception as e:
                print("Exception when visualizing angles:", e)
                error_handler(e)

            elapsed_time = time.time() - start_time
            print("Visualization time: " + str(elapsed_time) + " s")
        else:

            try:
                if config.draw_prediction_records:
                    self.output_record[str(self.count)] = [get_copy(acc_mu), get_copy(acc_pi), get_copy(acc_sigma),
                                                           get_copy(ang_probs),
                                                           get_copy(vel_mu), get_copy(vel_pi), get_copy(vel_sigma),
                                                           get_copy(lane_probs),
                                                           acc_label, steering_label, vel_label, lane_label,
                                                           acceleration]
            except Exception as e:
                print(e)
                exit(3)

    def visualize_hybrid_record(self):
        print('Visualizing prediction records')
        for step in self.output_record.keys():
            data = self.output_record[step]
            self.count = int(step)
            print('=> step', step)

            acc_mu = data[0]
            acc_pi = data[1]
            acc_sigma = data[2]
            ang_probs = data[3]
            vel_mu = data[4]
            vel_pi = data[5]
            vel_sigma = data[6]
            lane_probs = data[7]
            acc_label = data[8]
            steering_label = data[9]
            vel_label = data[10]
            lane_label = data[11]
            accelaration = data[12]

            config.visualize_inter_data = True
            self.visualize_hybrid_predictions(acc_pi, acc_mu, acc_sigma, ang_probs,
                                              vel_pi, vel_mu, vel_sigma, lane_probs,
                                              None,  # value
                                              acc_label, steering_label, vel_label, lane_label,
                                              None,  # value label
                                              accelaration, draw_truth=False, show_axis=False)
        print('done')

    def get_sm_probs(self, acc, ang, vel, lane):
        ang_probs = None
        acc_probs = None
        vel_probs = None
        lane_probs = None
        try:
            ang_probs = self.sm(ang)
            acc_probs = self.sm(acc)
            if config.use_vel_head:
                vel_probs = self.sm(vel)
            lane_probs = self.sm(lane)
        except Exception as e:
            print("Exception at calculating ang distribution:", e)
            error_handler(e)
        # print ("ang_probs", ang_probs)
        return acc_probs, ang_probs, vel_probs, lane_probs

    def get_labels_combined(self):
        steering_label = self.data_monitor.cur_data['true_steer']
        acc_label = self.data_monitor.cur_data['true_acc']
        vel_label = self.data_monitor.cur_data['true_vel']
        lane_label = self.data_monitor.cur_data['true_lane']

        return acc_label, steering_label, vel_label, lane_label

    def check_do_loop(self):
        do_loop = True
        self.dummy_count += 1
        if self.dummy_count % 1 != 0:
            do_loop = False
        return do_loop

    def publish_terminal_cmd(self):
        cmd_acc = Float32()
        cmd_steer = Float32()
        cmd_lane = Int32()

        cmd_acc.data = -config.max_acc
        cmd_steer.data = 0.0
        cmd_lane.data = 0
        # publish action and acc commands
        self.cmd_acc_pub.publish(cmd_acc)
        self.cmd_steer_pub.publish(cmd_steer)
        self.cmd_lane_pub.publish(cmd_lane)

        self.data_monitor.lock.release()
        self.update_steering = True
        self.data_monitor.update_steering = True
        self.data_monitor.update_data = True

    def get_encoded_labels(self, acc_label, steering_label, vel_label, lane_label):
        encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label = None, None, None, None
        try:
            encoded_steer_label = self.get_steer_label_onehot(steering_label)
            encoded_acc_label = self.get_acc_label_onehot(acc_label)
            encoded_vel_label = self.get_vel_label_onehot(vel_label)
            encoded_lane_label = self.get_lane_label_onehot(lane_label)
        except Exception as e:
            print("Exception when converting true label:", e)
            error_handler(e)

        return encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label

    def get_encoded_mdn_labels(self, acc_label, steering_label, vel_label, lane_label):
        encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label = None, None, None, None
        try:
            encoded_steer_label = self.get_mdn_steer_label_normalized(steering_label)
            encoded_acc_label = self.get_mdn_acc_label_normalized(acc_label)
            encoded_vel_label = self.get_mdn_vel_label_normalized(vel_label)
            encoded_lane_label = self.get_lane_label_onehot(lane_label)
        except Exception as e:
            print("Exception when converting true label:", e)
            error_handler(e)

        return encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label

    def get_encoded_hybrid_labels(self, acc_label, steering_label, vel_label, lane_label):
        encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label = None, None, None, None
        try:
            encoded_steer_label = self.get_steer_label_onehot(steering_label)
            encoded_acc_label = self.get_mdn_acc_label_normalized(acc_label)
            encoded_vel_label = self.get_mdn_vel_label_normalized(vel_label)
            encoded_lane_label = self.get_lane_label_onehot(lane_label)
        except Exception as e:
            print("Exception when converting true label:", e)
            error_handler(e)

        return encoded_acc_label, encoded_steer_label, encoded_vel_label, encoded_lane_label

    def get_lane_label_onehot(self, lane_label):
        lane_label_onehot = np.zeros(config.num_lane_bins, dtype=np.float32)
        if config.fit_lane or config.fit_action or config.fit_all:
            lane_label_np = float_to_np(lane_label)
            bin_idx = self.encode_lane_from_int(lane_label_np)

            if config.label_smoothing:
                lane_label_onehot = bin_idx
            else:
                lane_label_onehot[bin_idx] = 1  # one hot vector
        return lane_label_onehot

    def get_vel_label_onehot(self, vel_label):
        vel_label_onehot = np.zeros(config.num_vel_bins, dtype=np.float32)
        if config.fit_vel or config.fit_action or config.fit_all:
            vel_label_np = float_to_np(vel_label)
            bin_idx = self.encode_vel_from_raw(vel_label_np)

            if config.label_smoothing:
                vel_label_onehot = bin_idx
            else:
                vel_label_onehot[bin_idx] = 1  # one hot vector
        return vel_label_onehot

    def get_acc_label_onehot(self, acc_label):
        acc_label_onehot = np.zeros(config.num_acc_bins, dtype=np.float32)
        if config.fit_acc or config.fit_action or config.fit_all:
            acc_label_np = float_to_np(acc_label)
            bin_idx = self.encode_acc_from_id(acc_label_np)

            if config.label_smoothing:
                acc_label_onehot = bin_idx
            else:
                acc_label_onehot[bin_idx] = 1  # one hot vector
        return acc_label_onehot

    def get_steer_label_onehot(self, steering_label):
        steer_label_onehot = np.zeros(config.num_steering_bins, dtype=np.float32)
        if config.fit_ang or config.fit_action or config.fit_all:
            true_steering_label = np.degrees(steering_label)
            bin_idx = self.encode_steer_from_degree(true_steering_label)
            if config.label_smoothing:
                steer_label_onehot = bin_idx
            else:
                steer_label_onehot[bin_idx] = 1  # one hot vector
        return steer_label_onehot

    def get_mdn_vel_label_normalized(self, vel_label):
        vel_labels_normalized = np.zeros(1, dtype=np.float32)
        if config.fit_vel or config.fit_action or config.fit_all:
            vel_label_np = float_to_np(vel_label)
            vel_labels_normalized = self.encode_vel_from_raw(vel_label_np)
        return vel_labels_normalized

    def get_mdn_acc_label_normalized(self, acc_label):
        acc_label_normalized_np = np.zeros(1, dtype=np.float32)
        try:
            if config.fit_acc or config.fit_action or config.fit_all:
                acc_label_np = float_to_np(acc_label)
                acc_label_normalized_np = self.encode_acc_from_id(acc_label_np)
        except Exception as e:
            print(e)
            print("Exception when encoding true acc label")
            exit(1)

        return acc_label_normalized_np

    def get_mdn_steer_label_normalized(self, steering_label):
        steer_label_normalized_np = np.zeros(1, dtype=np.float32)
        if config.fit_ang or config.fit_action or config.fit_all:
            true_steering_label = np.degrees(steering_label)
            steer_label_normalized_np = self.encode_steer_from_degree(true_steering_label)
        return steer_label_normalized_np

    def inference(self):
        self.drive_net.eval()
        print("[inference] ")
        try:
            with torch.no_grad():
                X = self.get_current_data()
                self.data_monitor.update_data = True
                if config.model_type is "pytorch":
                    return forward_pass(X, self.count, self.drive_net, cmd_args, print_time=True, image_flag='test/')
                elif config.model_type is "jit":
                    return forward_pass_jit(X, self.count, self.drive_net, cmd_args, print_time=False,
                                            image_flag='test/')
        except Exception as e:
            error_handler(e)

    def get_current_data(self):
        input_images_np = self.data_monitor.cur_data['nn_input']
        data_len = input_images_np.shape[0]
        for i in range(0, data_len):
            input_images_np[i] = self.encode_input(input_images_np[i])
        input_tensor = torch.from_numpy(input_images_np)
        if config.visualize_val_data:
            pass
        input_tensor = input_tensor.to(device)
        return input_tensor


def print_model_size(model):
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print("No. parameters in model: %d", params)


from train import parse_cmd_args, update_global_config

if __name__ == '__main__':
    # Parsing training parameters
    config = global_params.config
    # Parsing training parameters
    cmd_args = parse_cmd_args()
    update_global_config(cmd_args)
    config.augment_data = False

    config.model_type = ''
    print("=> loading checkpoint '{}'".format(cmd_args.modelfile))
    try:
        checkpoint = torch.load(cmd_args.modelfile)
        load_settings_from_model(checkpoint, config, cmd_args)
        # Instantiate the NN model
        net = PolicyValueNet(cmd_args)
        print_model_size(net)
        net = nn.DataParallel(net, device_ids=[0]).to(device)  # device_ids= config.GPU_devices
        # Load parameters from checkpoint
        net.load_state_dict(checkpoint['state_dict'])
        print("=> model at epoch {}"
              .format(checkpoint['epoch']))
        config.model_type = "pytorch"
    except Exception as e:
        print(e)

    if config.model_type is not "pytorch" and config.model_type is not "jit":
        print("model is not pytorch or jit model!!!")
        exit(1)

    rospy.init_node('drive_net', anonymous=True)
    DriveController = DriveController(net)
    rospy.spin()
    # spin listner
