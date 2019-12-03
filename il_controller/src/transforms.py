# add all sorts of transforms here :)
import ipdb as pdb
import numpy as np

from Data_processing import global_params
from Data_processing.global_params import error_handler

config = global_params.config
import random
import sys

import matplotlib.pyplot as plt

counter = 0


def show_array(tensor):
    global counter
    try:
        # tensor = tensor.detach().cpu().numpy()
        fig, axarr = plt.subplots(1, 1)
        fig.set_figheight(6)
        fig.set_figwidth(6)
        axarr.imshow(tensor,
                     cmap='gray', interpolation='nearest')
        plt.tight_layout()
        fig.savefig("map_{}.png".format(counter), bbox_inches='tight', transparent=False)
        plt.close(fig)
        counter += 1

    except Exception as e:
        error_handler(e)
        exit(-1)
        # pdb.set_trace()


def validate_map(data, msg):
    v_max = np.max(data[0, config.channel_map])
    v_min = np.min(data[0, config.channel_map])
    print(msg + " map values: max %f, min %f" % (v_max, v_min))


def validate_map_array(map, msg):
    v_max = np.max(map)
    v_min = np.min(map)
    print(msg + " map values: max %f, min %f" % (v_max, v_min))


class PopulateImages(object):
    """
    Populate image arrays with sparse representations as indices and values

    """

    def __init__(self):
        pass

    def __call__(self, sample, doprint=False):
        # sample: 1 data point from the h5 table
        imsize = config.imsize
        num_agents = 1 + 0
        output_arr = np.zeros(
            (num_agents, config.total_num_channels, imsize, imsize), dtype=np.float32)
        for i in range(num_agents):
            self.copy_maps(i, output_arr, sample)  # Exo history.
            self.populate_lane(i, output_arr, sample)  # Lanes.
            try:
                self.populate_goal_and_hist_images(i, output_arr, sample)  # Goal + ego history.

            except Exception as e:
                error_handler(e)
                pdb.set_trace()

        # output_arr: dim (num_agents, congig.total_num_channels, imsize, imsize)
        acc_id_labels, ang_norm_labels, v_labels, vel_labels, lane_labels = self.get_labels(sample)

        if config.visualize_raw_data:
            # visualization.visualized_exo_agent_data(output_arr[i, config.channel_map[0]:config.channel_map[3]+1],
            #                                         root='Data_processing/')
            # visualization.visualize_image(output_arr[i, config.channel_lane], root='Data_processing/',
            #                               subfolder='h5_lane_image')
            from visualization import visualize_overlay_image
            visualize_overlay_image(output_arr[0, config.channel_lane],
                                                  output_arr[0, config.channel_map[0]],
                                                  output_arr[0, config.channel_map[3]],
                                                  lane_labels,
                                                  acc_id_labels,
                                                  root='Data_processing/',
                                                  subfolder='h5_overlay_image')
        cart_dat_arr = self.get_cart_data(sample)

        return output_arr, cart_dat_arr, v_labels, acc_id_labels, ang_norm_labels, vel_labels, lane_labels

    def get_labels(self, sample):
        v_labels = sample['value'][0]
        acc_id_labels = sample['acc_id'][0]
        ang_norm_labels = sample['steer_norm'][0]
        vel_labels = sample['vel'][config.label_cmdvel]
        lane_labels = sample['lane_change'][0]
        return acc_id_labels, ang_norm_labels, v_labels, vel_labels, lane_labels

    def get_cart_data(self, sample):
        return sample['cart_agents']

    def populate_lane(self, i, output_arr, sample):
        for point in sample['lane']:
            output_arr[i, config.channel_lane, int(
                point[0]), int(point[1])] = point[2]

    def populate_goal_and_hist_images(self, i, output_arr, sample):
        try:
            if i < 0:  # pedestrians, the number is set to 0
                agent_key = 'ped'
                src_entry = sample[agent_key][i]
            else:
                agent_key = 'car'
                src_entry = sample[agent_key]

            if config.use_goal_channel:
                for point in src_entry['goal']:
                    output_arr[i, config.channel_goal, int(
                        point[0]), int(point[1])] = point[2]

            if config.use_hist_channels:
                for ts in range(config.num_hist_channels):
                    for point in src_entry['hist'][ts]:
                        output_arr[i, config.channel_hist[ts], int(
                            point[0]), int(point[1])] = point[2]
            else:
                for ts in range(config.num_hist_channels):
                    # print("[transform] num points in src_entry['car_state'][ts]: {}".format(
                    # len(src_entry['car_state'][ts])))
                    for point in src_entry['car_state'][ts]:
                        # print("point {} {} intensity {}".format(point[0], point[1], point[2]))
                        output_arr[i, config.channel_map[ts], int(
                            point[0]), int(point[1])] = point[2]
                    # show_array(output_arr[i, config.channel_map[ts]])

        except Exception as e:
            print(e)

    def copy_maps(self, i, output_arr, sample):
        try:
            for ts in range(config.num_hist_channels):
                output_arr[i, config.channel_map[ts]] = sample['maps'][ts]
        except Exception as e:
            print(e)
        # validate_map(output_arr, "copy_maps")


def make_onehot(num_bins, bin_idx, prob):
    onehot_labels = np.zeros((num_bins), dtype=np.float32)
    onehot_labels[bin_idx] = prob
    return onehot_labels


def float_to_onehot(v, v_min, v_max, num_bins):
    try:
        if v > v_max:
            print("float_to_onehot warning: v > v_max, v={}".format(v), flush=True)
        elif v < v_min:
            print("float_to_onehot warning: v < v_min, v={}".format(v), flush=True)
        v = min(v_max - 0.0001, v)
        onehot_resolution = (v_max - v_min) / float(num_bins)
        bin_idx = int(np.floor(((v - v_min) / onehot_resolution)))

        if not config.label_smoothing:
            return bin_idx
        else:  # not used
            onehot_labels = np.zeros((num_bins), dtype=np.float32)
            eligible = 0
            if bin_idx + 1 < num_bins:
                eligible += 1
            if bin_idx - 1 >= 0:
                eligible += 1

            cpd = 0.8
            smoothing = 0.05
            onehot_labels[...] = smoothing / float(num_bins - 1 - eligible)
            onehot_labels[bin_idx] = cpd

            if bin_idx + 1 < num_bins:
                onehot_labels[bin_idx + 1] = (1.0 - smoothing - cpd) / float(eligible)
            if bin_idx - 1 >= 0:
                onehot_labels[bin_idx - 1] = (1.0 - smoothing - cpd) / float(eligible)
            return onehot_labels  # with label smoothing

    except Exception as e:
        error_handler(e)
        return None


def onehot_to_float(bin_idx, v_min, v_max, num_bins):
    try:
        resolution = (v_max - v_min) / float(num_bins)

        shift = v_min / resolution

        continous_bin = float(bin_idx) + shift + random.uniform(0.0, 1.0)
        v = continous_bin * resolution

    except Exception as e:
        error_handler(e)
        return None

    return v


acc_dict_id_to_raw = {0: 0.0,
                      1: config.max_acc,
                      2: -config.max_acc}


def ang_transform_degree_to_normalized(steer):
    ang = max(-config.max_steering_degree,
              min(steer, (config.max_steering_degree - 0.0001)))
    ang = ang / config.max_steering_degree
    return ang


def acc_transform_id_to_normalized(acc_id):
    try:
        acc_raw = acc_dict_id_to_raw[acc_id[0]]
        acc_raw = max(-config.max_acc,
                      min(acc_raw, config.max_acc - 0.0001))  # at max angle the bin index will be out of range
        acc_norm = acc_raw / config.max_acc
        return acc_norm
    except Exception as e:
        print("Exception at acc_transform")
        print(e)
        exit(1)


def vel_transform_raw_to_normalized(vel):
    vel = max(0.0, min(vel, config.vel_max - 0.0001))  # at max vel the bin index will be out of range
    vel = vel / config.vel_max
    return vel


def value_transform_raw_to_normalized(value):
    value = value / config.value_normalizer
    return value


def ang_transform_normalized_to_degree(steer_normalized):
    steer_degree = steer_normalized * config.max_steering_degree
    steer_degree = max(-config.max_steering_degree,
                       min(steer_degree, (config.max_steering_degree - 0.0001)))
    return steer_degree


def acc_transform_normalized_to_raw(acc_normalized):
    acc_raw = acc_normalized * config.max_acc
    acc_raw = max(-config.max_acc, min(acc_raw, config.max_acc))  # at max angle the bin index will be out of range

    return acc_raw


def vel_transform_normalized_to_raw(vel):
    vel = vel * config.vel_max
    vel = max(0.0, min(vel, config.vel_max - 0.0001))  # at max vel the bin index will be out of range
    return vel


def value_transform_normalized_to_raw(value):
    value = value * config.value_normalizer
    return value


def float_to_np(v):
    try:
        v_np = np.zeros(1, dtype=np.float32)
        v_np[0] = v
        return v_np

    except Exception as e:
        print("Exception at float_to_np")
        print(e)
        exit(1)


class MdnSteerEncoderDegree2Normalized(object):
    def __call__(self, steer_degree):
        ang = ang_transform_degree_to_normalized(steer_degree)
        return float_to_np(ang)


class MdnAccEncoderID2Normalized(object):
    def __call__(self, acc):
        acc = acc_transform_id_to_normalized(acc)
        return float_to_np(acc)


class MdnVelEncoderRaw2Normalized(object):
    def __call__(self, vel):
        vel = vel_transform_raw_to_normalized(vel)
        return float_to_np(vel)


class ValueEncoderRaw2Normalized(object):
    """Populates labels and outputs in desired format and shape
    Rescale value
    """

    def __init__(self):
        pass

    def __call__(self, v_label):
        # scale down v_labels
        v_label = value_transform_raw_to_normalized(v_label)
        return v_label


class InputEncoder(object):
    """Populates labels and outputs in desired format and shape
    Rescale value
    """

    def __init__(self):
        self.normalize = Normalize()
        pass

    def __call__(self, input_data):
        # normalize input images
        #
        input_data = self.normalize(input_data)
        return input_data


class SteerEncoderDegreeToOnehot(object):
    """Populates labels and outputs in desired format and shape
    Convert steering angle from degree value to one-hot vector encoding
    """

    def __init__(self):
        # steering angles are categorized into bins
        self.num_steering_bins = config.num_steering_bins
        pass

    def __call__(self, ang):
        # input angle in degrees
        # clip ang to max_steering range
        #
        ang = ang_transform_degree_to_normalized(ang)

        bin_idx = float_to_onehot(v=ang, v_min=-config.steer_normalized_limit, v_max=config.steer_normalized_limit,
                                  num_bins=self.num_steering_bins)

        return bin_idx


class LaneEncoderIntToOnehot(object):
    """Populates labels and outputs in desired format and shape
    Convert lane change decision from (-1, 0, 1) to one-hot vector encoding
    """

    def __init__(self):
        # steering angles are categorized into bins
        self.num_bins = config.num_lane_bins
        pass

    def __call__(self, lane):
        bin_idx = float_to_onehot(v=lane, v_min=-1.0, v_max=1.0,
                                  num_bins=self.num_bins)
        return bin_idx


class AccEncoderIDToOnehot(object):
    """Populates labels and outputs in desired format and shape
    """

    def __init__(self):
        self.num_acc_bins = config.num_acc_bins
        pass

    def __call__(self, acc_id):
        # pdb.set_trace()
        # convert acc_id to one-hot vector

        # print("raw acc: {}".format(acc))
        acc_norm = acc_transform_id_to_normalized(acc_id)
        # print("transformed acc: {}".format(acc))
        bin_idx = float_to_onehot(v=acc_norm, v_min=-1.0, v_max=1.0, num_bins=self.num_acc_bins)
        # print("bin_idx: {}".format(bin_idx))
        return bin_idx


class VelEncoderRaw2Onehot(object):
    """Populates labels and outputs in desired format and shape
    """

    def __init__(self):
        self.bins = config.num_vel_bins
        self.num_bins = self.bins
        self.vel_max = config.vel_max
        pass

    def __call__(self, vel):
        # pdb.set_trace()
        # convert velocity to bin_index or one-hot vector        

        vel = vel_transform_raw_to_normalized(vel)

        bin_idx = float_to_onehot(v=vel, v_min=0.0, v_max=1.0, num_bins=self.num_bins)

        return bin_idx


class MdnSteerDecoderNormalized2Degree(object):
    def __call__(self, ang):
        steer = ang_transform_normalized_to_degree(ang)
        return steer


class MdnAccDecoderNormalized2Raw(object):
    def __call__(self, acc):
        acc = acc_transform_normalized_to_raw(acc)
        return acc


class MdnVelDecoderNormalized2Raw(object):
    def __call__(self, vel):
        vel = vel_transform_normalized_to_raw(vel)
        return vel


class SteerDecoderOnehot2Degree(object):
    """
    Convert one-hot vector encoding to steering angle from degree value
    """

    def __init__(self):
        # steering angles are categorized into bins
        #
        self.num_steering_bins = config.num_steering_bins
        pass

    def __call__(self, bin_idx):
        # input angle in degrees
        # clip ang to max_steering range
        #
        steer = onehot_to_float(bin_idx=bin_idx,
                                v_min=-config.steer_normalized_limit, v_max=config.steer_normalized_limit,
                                num_bins=self.num_steering_bins)
        steer = ang_transform_normalized_to_degree(steer)

        return steer


class AccDecoderOnehot2Raw(object):
    """Populates labels and outputs in desired format and shape
    """

    def __init__(self):
        self.num_acc_bins = config.num_acc_bins
        pass

    def __call__(self, bin_idx):
        # convert acc_id to one-hot vector
        #
        acc = onehot_to_float(bin_idx=bin_idx, v_min=-1.0, v_max=1.0, num_bins=self.num_acc_bins)
        acc = acc_transform_normalized_to_raw(acc)

        return acc


class VelDecoderOnehot2Raw(object):
    """Populates labels and outputs in desired format and shape
    """

    def __init__(self):
        self.bins = config.num_vel_bins
        self.num_bins = self.bins
        self.vel_max = config.vel_max
        pass

    def __call__(self, bin_idx):
        vel = onehot_to_float(bin_idx=bin_idx, v_min=0.0, v_max=1.0, num_bins=self.num_bins)
        vel = vel_transform_normalized_to_raw(vel)
        return vel


class LaneDecoderOnehot2Int(object):
    """Populates labels and outputs in desired format and shape
    """

    def __init__(self):
        pass

    def __call__(self, bin_idx):
        if bin_idx == 0:
            return -1
        if bin_idx == 1:
            return 0
        if bin_idx == 2:
            return 1
        return None


class Fliplr(object):
    """Flips all arrays in left-right direction
    """

    def __init__(self):
        pass

    def __call__(self, input, steer, lane):
        # input: dim (num_agents, congig.total_num_channels, imsize, imsize)
        #
        output = np.zeros_like(input, dtype=np.float32)
        for i in range(1 + 0):
            for j in range(config.total_num_channels):
                output[i, j] = np.fliplr(input[i, j])

        return output, -steer, -lane  # flip the steering


class Flipud(object):
    """Flips all arrays in up-down direction
    """

    def __init__(self):
        pass

    def __call__(self, input, steer, lane):
        # input: dim (num_agents, congig.total_num_channels, imsize, imsize)
        #
        output = np.zeros_like(input, dtype=np.float32)
        for i in range(1 + 0):
            for j in range(config.total_num_channels):
                output[i, j] = np.flipud(input[i, j])
        return output, -steer, -lane  # flip the steering


class Rot(object):
    """Rotates all arrays 1/2/3 times
    """

    def __init__(self, amount):
        self.amount = amount

    def __call__(self, input, steer, lane):
        # input: dim (num_agents, congig.total_num_channels, imsize, imsize)
        output = np.zeros_like(input, dtype=np.float32)
        for i in range(1 + 0):
            for j in range(config.total_num_channels):
                output[i, j] = np.rot90(input[i, j], self.amount)

        return output, steer, lane


class FlipRot(object):
    """FLip  1 time and rotates all arrays 1/2/3 times
    """

    def __init__(self, amount):
        self.amount = amount

    def __call__(self, input, steer, lane):
        # input: dim (num_agents, congig.total_num_channels, imsize, imsize)
        output = np.zeros_like(input, dtype=np.float32)
        for i in range(1 + 0):
            for j in range(config.total_num_channels):
                output[i, j] = np.rot90(np.flipud(input[i, j]), self.amount)

        return output, -steer, -lane


class Identity(object):
    """returns inputs as it is
    """

    def __init__(self):
        pass

    def __call__(self, input, steer, lane):
        return input, steer, lane


class Normalize(object):
    '''normalizes goal,hist1 and hist2 channels
    '''

    def __init__(self):
        pass

    def __call__(self, input):
        num_agents = 1 + 0
        for i in range(num_agents):
            for c in range(config.total_num_channels):
                maxval = np.max(input[i, c])
                if maxval != 0:
                    input[i, c] = input[i, c] / maxval

        return input
