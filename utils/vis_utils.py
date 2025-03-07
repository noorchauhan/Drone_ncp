import json
import os
from pathlib import Path
from typing import Optional, Callable, Sequence, Union, Dict, Any, Iterable

import cv2
import numpy as np
import pandas as pd
from PIL import Image
from numpy import ndarray
from pandas import DataFrame
from tensorflow import Tensor
from tensorflow.python.keras.models import Functional
from tqdm import tqdm

from keras_models import IMAGE_SHAPE, IMAGE_SHAPE_CV
from utils.data_utils import image_dir_generator
from utils.model_utils import generate_hidden_list, NCPParams, LSTMParams, CTRNNParams, TCNParams

TEXT_BOX_HEIGHT = 30
ARROW_BOX_HEIGHT = 100 + TEXT_BOX_HEIGHT
VEL_MAX = 2
YAW_MAX = 2
ARROW_COLOR = (0, 255, 0)
ARROW_THICKNESS = 4

GRID_BORDER_WIDTH = 2


# from https://stackoverflow.com/questions/37921295/python-pil-image-make-3x3-grid-from-sequence-images
def image_grid(imgs: Sequence[Tensor], rows: int, cols: int):
    pil_imgs = []
    for ten in imgs:
        np_im = ten.numpy()
        # normalize each image separately
        np_im = cv2.normalize(np_im, dst=None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        pil_imgs.append(Image.fromarray(np_im))
    w, h = pil_imgs[0].size
    mod_w = w + GRID_BORDER_WIDTH
    mod_h = h + GRID_BORDER_WIDTH

    # init grid fully red for borders
    grid = np.zeros((rows * mod_h - GRID_BORDER_WIDTH, cols * mod_w - GRID_BORDER_WIDTH, 3), dtype=np.uint8)
    grid[..., -1] = 255  # paint grid red for borders
    grid = Image.fromarray(grid)

    for i, img in enumerate(pil_imgs):
        grid.paste(img, box=(i % cols * mod_w, i // cols * mod_h))
    return np.array(grid)


def show_vel_cmd(vel_cmd: ndarray, img_width: int):
    """
    Draws arrows corresponding to the values in vel_cmd. The arrows correspond to the directions on a mode 2 rc
    controller. The left arrow vertical is throttle and horizontal is yaw. The right arrow vertical is pitch and the
    horizontal is roll

    :param vel_cmd: ndarray of shape 1x4 with commands pitch, roll, throt, yaw in front, left, up, counterclockwise
    :param img_width: width of image to be returned
    :return: Image with both vel_cmd text and arrows drawn on it
    """
    vel_text = show_vel_text(vel_cmd, img_width)
    vel_cmd = np.squeeze(vel_cmd, axis=0)
    arrow_img = np.zeros((ARROW_BOX_HEIGHT - TEXT_BOX_HEIGHT, img_width, 3), dtype=np.uint8)
    left_stick_x = img_width // 3
    right_stick_x = img_width * 2 // 3
    arrow_y = ARROW_BOX_HEIGHT // 2
    max_arrow_len = img_width // 3
    arrow_scale = max_arrow_len / VEL_MAX

    yaw_img = cv2.arrowedLine(arrow_img, (left_stick_x, arrow_y),
                              (left_stick_x + int(-vel_cmd[3] * arrow_scale), arrow_y), ARROW_COLOR, ARROW_THICKNESS)
    throt_img = cv2.arrowedLine(yaw_img, (left_stick_x, arrow_y),
                                (left_stick_x, arrow_y + int(-vel_cmd[2] * arrow_scale)), ARROW_COLOR, ARROW_THICKNESS)
    roll_img = cv2.arrowedLine(throt_img, (right_stick_x, arrow_y),
                               (right_stick_x + int(-vel_cmd[1] * arrow_scale), arrow_y), ARROW_COLOR, ARROW_THICKNESS)
    pitch_img = cv2.arrowedLine(roll_img, (right_stick_x, arrow_y),
                                (right_stick_x, arrow_y + int(-vel_cmd[0] * arrow_scale)), ARROW_COLOR, ARROW_THICKNESS)

    return np.concatenate([vel_text, pitch_img], axis=0)


def show_vel_text(vel_cmd: ndarray, img_width: int):
    """
    Draws vel_cmd as text on an image for visualization
    :param vel_cmd: ndarray of shape 1xn that has velocity to display
    :param img_width: width of image that should be returned
    :return: Image with vel_cmd drawn on as text
    """
    text_img = np.zeros((TEXT_BOX_HEIGHT, img_width, 3), dtype=np.uint8)
    vel_rounded = str([round(vel, 2) for vel in vel_cmd[0]])
    cv2.putText(text_img, vel_rounded, (0, TEXT_BOX_HEIGHT // 2), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1,
                cv2.LINE_AA)
    return text_img


def run_visualization(vis_model: Functional, data: Union[str, Iterable], vis_func: Callable,
                      image_output_path: Optional[str] = None,
                      video_output_path: Optional[str] = None, reverse_channels: bool = True,
                      control_source: Union[str, Functional, None] = None, absolute_norm: bool = True,
                      vis_kwargs: Optional[Dict[str, Any]] = None) -> Sequence[ndarray]:
    """
    Runner script that loads images, runs VisualBackProp, and saves saliency maps
    """
    if vis_kwargs is None:
        vis_kwargs = {}

    if isinstance(data, str):
        data = image_dir_generator(data, IMAGE_SHAPE, reverse_channels)

    # create output_dir if not present
    # if image_output_path is not None:
        # Path(image_output_path).mkdir(parents=True, exist_ok=True)
    if video_output_path is not None:
        Path(os.path.dirname(video_output_path)).mkdir(parents=True, exist_ok=True)

    if len(vis_model.inputs) > 1:
        vis_hiddens = generate_hidden_list(vis_model, False)
    else:
        # vis_model doesn't need hidden state
        vis_hiddens = [None]

    if isinstance(control_source, Functional):
        control_hiddens = generate_hidden_list(control_source, True)
    elif isinstance(control_source, str):
        control_source = pd.read_csv(control_source)

    saliency_imgs = []
    og_imgs = []
    extra_imgs = []
    controls = []
    csv_healthy = True
    for i, img in tqdm(enumerate(data)):
        og_imgs.append(img)
        saliency, vis_hiddens, sample_extra = vis_func(img, vis_model, vis_hiddens, **vis_kwargs)
        saliency_imgs.append(saliency)
        extra_imgs.append(sample_extra)

        if control_source is not None:
            if isinstance(control_source, Functional):
                out = control_source.predict([img, *control_hiddens])
                vel_cmd = out[0]
                control_hiddens = out[1:]  # list num_hidden long, each el is batch x hidden_dim
            elif isinstance(control_source, DataFrame):
                try:
                    vel_cmd = np.nan_to_num(
                        control_source.iloc[i][["cmd_vx", "cmd_vy", "cmd_vz", "cmd_omega"]].to_numpy())
                except IndexError:
                    vel_cmd = np.zeros((4,))
                    if csv_healthy:
                        # log error
                        csv_healthy = False
                        csv_rows = control_source.shape[0]
                        image_num = len([c for c in os.listdir(data) if 'png' in c])
                        print(f"Warning: CSV for {data} has {csv_rows} rows and {image_num} images")

                vel_cmd = np.expand_dims(vel_cmd, axis=0)
            else:
                raise ValueError(f"Unsupported control source {control_source}")
            controls.append(vel_cmd)

    # normalize and display saliency images
    video_frames = []
    data_list = zip(range(len(saliency_imgs)), og_imgs, saliency_imgs, extra_imgs,
                    controls if controls else [None] * len(saliency_imgs))

    # calculate absolute min and max
    saliency_min = None
    saliency_max = None
    if absolute_norm:
        saliency_ndarr = np.asarray(saliency_imgs)
        saliency_min = np.min(saliency_ndarr)
        saliency_max = np.max(saliency_ndarr)
    # prepare video frames
    saliency_written = []
    for i, img, saliency, extra, vel_cmd in data_list:
        #cv2.imshow('asd', np.array(saliency))
        #print(saliency.shape)
        #print(IMAGE_SHAPE_CV)
        saliency_writeable = convert_to_color_frame(saliency, desired_size=IMAGE_SHAPE_CV, min_value=saliency_min,
                                                    max_value=saliency_max)
        #cv2.imshow('asde', np.array(saliency_writeable))
        #cv2.waitKey(0)
        #print(saliency_writeable.shape)
        if image_output_path:
            cv2.imwrite(f"{image_output_path}_saliency_mask_{i}.png", saliency_writeable)

        # display OG frame and saliency map stacked top and bottom
        og_int = np.squeeze(np.uint8(img), axis=0)

        # when opening with PIL and writing with cv video writer, channels are implicitly flipped
        # if not flipped, need to flip if OG and if is flipped, need to flip again to restore normal
        og_int = og_int[..., ::-1]
        img_stack = [og_int, saliency_writeable]

        # if extra is not None:
        #     img_stack.extend(extra)

        if vel_cmd is not None:
            text_img = show_vel_cmd(vel_cmd, og_int.shape[1])
            img_stack.append(text_img)

        stacked_imgs = np.concatenate(img_stack, axis=0)
        video_frames.append(stacked_imgs)

    # write video
    if video_output_path:
        write_video(img_seq=video_frames, output_path=video_output_path)

    return saliency_written


def write_video(img_seq: Sequence[ndarray], output_path: str, fps: int = 10):
    Path(os.path.dirname(output_path)).mkdir(exist_ok=True, parents=True)
    seq_shapes = [img.shape for img in img_seq]
    assert seq_shapes.count(seq_shapes[0]) == len(seq_shapes), "Not all shapes in img_seq are the same"

    image_shape = img_seq[0].shape
    cv_shape = (image_shape[1], image_shape[0])  # videowriter takes width, height, image_shape is height, width
    writer = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc('m', 'p', '4', 'v'), fps, cv_shape,
                             True)  # true means write color frames

    for img in img_seq:
        writer.write(img)

    writer.release()


def parse_params_json(params_path: str, set_single_step: bool = True):
    with open(params_path, "r") as f:
        params_data = json.loads(f.read())

    for local_path, params_str in params_data.items():
        model_params: Union[NCPParams, LSTMParams, CTRNNParams, TCNParams, None] = eval(params_str)
        if set_single_step:
            model_params.single_step = True
        model_path = os.path.join(os.path.dirname(params_path), local_path)
        yield local_path, model_path, model_params


def convert_to_color_frame(saliency_map: Union[Tensor, ndarray], desired_size: Optional[Sequence[int]] = None,
                           min_value: Optional[float] = None, max_value: Optional[float] = None,
                           color_map: int = cv2.COLORMAP_INFERNO) -> ndarray:
    """
    Converts tensorflow tensor (1 channel) to 3-channel grayscale numpy array for use with OpenCV
    """
    assert (min_value is None) == (max_value is None), "Pass both min and max or neither"
    if isinstance(saliency_map, Tensor):
        saliency_map = saliency_map.numpy()
    # add dummy color channel if not present
    if len(saliency_map.shape) == 2:
        saliency_map = np.expand_dims(saliency_map, axis=-1)

    # if grayscale image, repeat to virtually make color
    if saliency_map.shape[-1] == 1:
        saliency_map = np.repeat(saliency_map, 3, axis=-1)

    # resize to desired size if specified
    if desired_size is not None:
        saliency_map = cv2.resize(saliency_map, desired_size, )

    if min_value is not None and max_value is not None:
        saliency_map = saliency_map - min_value
        saliency_map = saliency_map / (max_value - min_value) * 255
        saliency_map = saliency_map.astype(np.uint8)
    else:
        saliency_map = cv2.normalize(saliency_map, dst=None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX,
                                     dtype=cv2.CV_8U)
    # apply color map, matplotlib and cv2 color conventions are reversed, so flip channels
    saliency_map = cv2.applyColorMap(saliency_map, color_map)[..., ::-1]
    return saliency_map
