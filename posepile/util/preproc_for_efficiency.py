"""Functions to transform (reproject, i.e. scale and crop) images as a preprocessing step.
This helps us avoid loading and decoding the full JPEG images at training time.
Instead, we just load the much smaller cropped and resized images.
"""
import os

os.environ['OMP_NUM_THREADS'] = '1'
import copy

import boxlib
import cameralib
import cv2
import imageio.v2 as imageio
import numpy as np
import rlemasklib
import simplepyutils as spu

import posepile.util.improc as improc
from posepile import util


def make_efficient_example(
        ex, new_image_path, further_expansion_factor=1,
        image_adjustments_3dhp=False, min_time=None, ignore_broken_image=False,
        horizontal_flip=False, downscale_input_for_antialias=False, extreme_perspective=False,
        joint_info=None):
    """Make example by storing the image in a cropped and resized version for efficient loading"""
    is3d = hasattr(ex, 'world_coords')
    w, h = (improc.image_extents(util.ensure_absolute_path(ex.image_path))
            if isinstance(ex.image_path, str)
            else (ex.image_path.shape[1], ex.image_path.shape[0]))
    full_box = boxlib.full(imsize=[w, h])

    has_3d_camera = hasattr(ex, 'camera') and ex.camera is not None
    if has_3d_camera:
        old_camera = ex.camera
        new_camera = ex.camera.copy()
        new_camera.turn_towards(target_image_point=boxlib.center(ex.bbox))
        new_camera.undistort()
    else:
        old_camera = cameralib.Camera.create2D()
        new_camera = old_camera.copy()

    reprojected_box = cameralib.reproject_box_side_midpoints(ex.bbox, old_camera, new_camera)
    if extreme_perspective:
        reprojected_full_box = np.array([-1e9, -1e9, 2e9, 2e9], np.float32)
    else:
        reprojected_full_box = cameralib.reproject_box_corners(full_box, old_camera, new_camera)
    expanded_bbox = (get_expanded_crop_box(
        reprojected_box, reprojected_full_box, further_expansion_factor)
                     if further_expansion_factor > 0 else reprojected_box)
    scale_factor = min(1.2, 256 / np.max(reprojected_box[2:]) * 1.5)
    new_camera.shift_image(-expanded_bbox[:2])
    new_camera.scale_output(scale_factor)

    reprojected_box = cameralib.reproject_box_side_midpoints(ex.bbox, old_camera, new_camera)
    dst_shape = spu.rounded_int_tuple(scale_factor * expanded_bbox[[3, 2]])

    new_image_abspath = util.ensure_absolute_path(new_image_path)

    if (not (spu.is_file_newer(new_image_abspath, min_time)
             and improc.is_image_readable(new_image_abspath))):
        try:
            im = (improc.imread(ex.image_path)
                  if isinstance(ex.image_path, str) else ex.image_path)
        except Exception as exception:
            if ignore_broken_image and not isinstance(exception, FileNotFoundError):
                return None
            else:
                raise
        if horizontal_flip:
            im = im[:, ::-1]
        im = np.power((im.astype(np.float32) / 255), 2.2)

        if downscale_input_for_antialias:
            input_scale_factor = min(1, scale_factor * 4)
            im = improc.resize_by_factor(im, input_scale_factor)
            old_camera_scaled = old_camera.copy()
            old_camera_scaled.scale_output(input_scale_factor)
        else:
            old_camera_scaled = old_camera

        new_im = cameralib.reproject_image(
            im, old_camera_scaled, new_camera, dst_shape, antialias_factor=2,
            interp=cv2.INTER_CUBIC)
        new_im = np.clip(new_im, 0, 1)

        if image_adjustments_3dhp:
            # enhance the 3dhp images to reduce the green tint and increase brightness
            new_im = (new_im ** (1 / 2.2 * 0.67) * 255).astype(np.uint8)
            new_im = improc.white_balance(new_im, 110, 145)
        else:
            new_im = (new_im ** (1 / 2.2) * 255).astype(np.uint8)
        spu.ensure_parent_dir_exists(new_image_abspath)
        imageio.imwrite(new_image_abspath, new_im, quality=95)
        # assert improc.is_image_readable(new_image_abspath)
    new_ex = copy.deepcopy(ex)
    new_ex.bbox = reprojected_box
    new_ex.image_path = new_image_path

    if has_3d_camera:
        new_ex.camera = new_camera

    if not is3d:
        new_ex.coords = cameralib.reproject_image_points(new_ex.coords, old_camera, new_camera)

    if hasattr(ex, 'mask') and ex.mask is not None:
        if isinstance(ex.mask, str):
            mask = improc.imread(util.ensure_absolute_path(ex.mask))[..., 0]
            mask_reproj = cameralib.reproject_image(
                mask, ex.camera, new_camera, dst_shape, antialias_factor=2)
            mask_reproj = 255 * (mask_reproj > 32 / 255).astype(np.uint8)
            new_ex.mask = get_connected_component_with_highest_iou(mask_reproj, reprojected_box)
        elif isinstance(ex.mask, np.ndarray):
            mask_reproj = cameralib.reproject_image(
                ex.mask, ex.camera, new_camera, dst_shape, antialias_factor=2)
            new_ex.mask = rlemasklib.encode(mask_reproj > 127)
        else:
            mask = rlemasklib.decode(ex.mask) * 255
            new_mask = cameralib.reproject_image(
                mask, ex.camera, new_camera, dst_shape, antialias_factor=2)
            new_ex.mask = rlemasklib.encode(new_mask > 127)
    return new_ex


def get_expanded_crop_box(bbox, full_box, further_expansion_factor):
    max_rotate = np.pi / 6
    padding_factor = 1 / 0.85
    scale_down_factor = 1 / 0.85
    shift_factor = 1.1
    s, c = np.sin(max_rotate), np.cos(max_rotate)
    w, h = bbox[2:]
    box_center = boxlib.center(bbox)
    rot_bbox_side = max(c * w + s * h, c * h + s * w)
    rot_bbox = boxlib.box_around(box_center, rot_bbox_side)
    expansion_factor = (
            padding_factor * shift_factor * scale_down_factor * further_expansion_factor)
    expanded_bbox = boxlib.intersection(
        boxlib.expand(rot_bbox, expansion_factor), full_box)
    return expanded_bbox


def get_connected_component_with_highest_iou(mask, person_box):
    """Finds the 4-connected component in `mask` with the highest bbox IoU with the `person box`"""
    mask = mask.astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 4, cv2.CV_32S)
    component_boxes = stats[:, :4]
    ious = [boxlib.iou(component_box, person_box) for component_box in component_boxes]
    person_label = np.argmax(ious)
    return rlemasklib.encode(labels == person_label)
