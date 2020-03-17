"""Representation of the standard COCO json dataset format.
When working with a new dataset, we strongly suggest to convert the dataset into
the COCO json format and use the existing code; it is not recommended to write
code to support new dataset formats.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import copy
import pickle
import numpy as np
import os
import scipy.sparse

# Must happen before importing COCO API (which imports matplotlib)
"""Set matplotlib up."""
import matplotlib
# Use a non-interactive backend
matplotlib.use('Agg')
# COCO API
from pycocotools import mask as COCOmask
from pycocotools.coco import COCO

from sognet.config.config import config

from sognet.lib.utils.timer import Timer
import sognet.bbox.bbox_transform as box_utils
import sognet.mask.mask_transform as segm_utils

from sognet.lib.utils.logging import logger

class JsonDataset(object):
    """A class representing a COCO json dataset."""

    def __init__(self, name, image_dir, anno_file, pan_ins_file=None):
        if logger:
            logger.info('Creating: {}'.format(name))
        self.pan_ins_file = pan_ins_file
        self.name = name
        self.image_directory = image_dir
        self.image_prefix = ''
        self.COCO = COCO(anno_file)
        if self.pan_ins_file is not None:
            self.pan_coco = COCO(pan_ins_file)
        self.debug_timer = Timer()
        # Set up dataset classes
        category_ids = self.COCO.getCatIds()
        categories = [c['name'] for c in self.COCO.loadCats(category_ids)]
        self.category_to_id_map = dict(zip(categories, category_ids))
        self.classes = ['__background__'] + categories
        self.num_classes = len(self.classes)
        self.json_category_id_to_contiguous_id = {
            v: i + 1
            for i, v in enumerate(self.COCO.getCatIds())
        }
        self.contiguous_category_id_to_json_id = {
            v: k
            for k, v in self.json_category_id_to_contiguous_id.items()
        }
        # self._init_keypoints()
        self.keypoints = None

    def get_roidb(
        self,
        gt=False,
        proposal_file=None,
        min_proposal_size=2,
        proposal_limit=-1,
        crowd_filter_thresh=0
    ):
        """Return an roidb corresponding to the json dataset. Optionally:
           - include ground truth boxes in the roidb
           - add proposals specified in a proposals file
           - filter proposals based on a minimum side length
           - filter proposals that intersect with crowd regions
        """
        assert gt is True or crowd_filter_thresh == 0, \
            'Crowd filter threshold must be 0 if ground-truth annotations ' \
            'are not included.'
        image_ids = self.COCO.getImgIds()
        image_ids.sort()
        # if len(image_ids) > 5000:
        #     image_ids = 69816
        if config.debug_mode:
            # roidb = copy.deepcopy(self.COCO.loadImgs(image_ids))
            roidb = copy.deepcopy(self.COCO.loadImgs(image_ids))[:10]
        else:
            roidb = copy.deepcopy(self.COCO.loadImgs(image_ids))
        for entry in roidb:
            self._prep_roidb_entry(entry)
        if gt:
            # Include ground-truth object annotations
            self.debug_timer.tic()
            for entry in roidb:
                self._add_gt_annotations(entry)
            if logger:
                logger.debug(
                    '_add_gt_annotations took {:.3f}s'.
                    format(self.debug_timer.toc(average=False))
                )
        if proposal_file is not None:
            # Include proposals from a file
            self.debug_timer.tic()
            self._add_proposals_from_file(
                roidb, proposal_file, min_proposal_size, proposal_limit,
                crowd_filter_thresh
            )
            if logger:
                logger.debug(
                    '_add_proposals_from_file took {:.3f}s'.
                    format(self.debug_timer.toc(average=False))
                )
        _add_class_assignments(roidb)
        return roidb

    def _prep_roidb_entry(self, entry):
        """Adds empty metadata fields to an roidb entry."""
        # Reference back to the parent dataset
        entry['dataset'] = self
        # Make file_name an abs path
        entry['image'] = os.path.join(
            self.image_directory, self.image_prefix + entry['file_name']
        )
        entry['flipped'] = False
        entry['has_visible_keypoints'] = False
        # Empty placeholders
        entry['boxes'] = np.empty((0, 4), dtype=np.float32)
        entry['segms'] = []
        entry['matched_segms'] = []
        entry['gt_classes'] = np.empty((0,), dtype=np.int32)
        entry['seg_areas'] = np.empty((0,), dtype=np.float32)
        entry['gt_overlaps'] = scipy.sparse.csr_matrix(
            np.empty((0, self.num_classes), dtype=np.float32)
        )
        entry['is_crowd'] = np.empty((0,), dtype=np.bool)
        # 'box_to_gt_ind_map': Shape is (#rois). Maps from each roi to the index
        # in the list of rois that satisfy np.where(entry['gt_classes'] > 0)
        entry['box_to_gt_ind_map'] = np.empty((0,), dtype=np.int32)
        if self.keypoints is not None:
            entry['gt_keypoints'] = np.empty(
                (0, 3, self.num_keypoints), dtype=np.int32
            )
        # Remove unwanted fields that come from the json file (if they exist)
        for k in ['date_captured', 'url', 'license']:
            if k in entry:
                del entry[k]

    def cal_iou(self, bbox1, bboxes):
        bbox1 = np.array(bbox1)
        bboxes = np.array(bboxes)
        area1 = (bbox1[2] - bbox1[0] + 1) * (bbox1[3] - bbox1[1] + 1)
        areas = (bboxes[:, 2] - bboxes[:, 0] + 1) * (bboxes[:, 3] - bboxes[:, 1] + 1)
        x1 = np.maximum(bbox1[0], bboxes[:, 0])
        y1 = np.maximum(bbox1[1], bboxes[:, 1])
        x2 = np.minimum(bbox1[2], bboxes[:, 2])
        y2 = np.minimum(bbox1[3], bboxes[:, 3])
        w = np.maximum(x2 - x1 + 1, 0)
        h = np.maximum(y2 - y1 + 1, 0)
        inter = w * h
        iou = inter / (area1 + areas - inter)
        # print(iou)
        # print(bbox1)
        # print('\n')
        # for x in bboxes:
        #     print(x)
        return iou

    def match_ins_pan_ins(self, ins_boxes, pan_boxes, pan_ins_mask):
        matched_mask = [None] * len(ins_boxes)
        matched_idx = list()
        for i, box in enumerate(pan_boxes):
            iou = self.cal_iou(box, ins_boxes)
            idx = iou.argmax()
            max_iou = iou.max()
            if idx in matched_idx:
                # print("1 vs 2")
                if max_iou < 0.5:
                    continue
            matched_idx.append(idx)
            matched_mask[idx] = pan_ins_mask[i]
            
        return matched_mask

    def _add_gt_annotations(self, entry):
        """Add ground truth annotation metadata to an roidb entry."""
        ann_ids = self.COCO.getAnnIds(imgIds=entry['id'], iscrowd=None)
        pan_ann_ids = self.pan_coco.getAnnIds(imgIds=entry['id'], iscrowd=None)
        objs = self.COCO.loadAnns(ann_ids)
        pan_objs = self.pan_coco.loadAnns(pan_ann_ids)
        # print(len(objs), len(pan_objs))
        # Sanitize bboxes -- some are invalid
        valid_objs = []
        valid_segms = []
        pan_valid_bboxes = []
        pan_valid_segms = []

        width = entry['width']
        height = entry['height']
        for obj in pan_objs:
            # crowd regions are RLE encoded and stored as dicts
            if isinstance(obj['segmentation'], list):
                # Valid polygons have >= 3 points, so require >= 6 coordinates
                obj['segmentation'] = [
                    p for p in obj['segmentation'] if len(p) >= 6
                ]
            # Convert form (x1, y1, w, h) to (x1, y1, x2, y2)
            x1, y1, x2, y2 = box_utils.xywh_to_xyxy(obj['bbox'])
            x1, y1, x2, y2 = box_utils.clip_xyxy_to_image(
                x1, y1, x2, y2, height, width
            )
            # Require non-zero seg area and more than 1x1 box size
            if obj['area'] > 0 and x2 > x1 and y2 > y1:
                obj['clean_bbox'] = [x1, y1, x2, y2]
                pan_valid_bboxes.append(obj['clean_bbox'])
                pan_valid_segms.append(obj['segmentation'])
            # else:
                # print("delete")

        for obj in objs:
            # crowd regions are RLE encoded and stored as dicts
            if isinstance(obj['segmentation'], list):
                # Valid polygons have >= 3 points, so require >= 6 coordinates
                obj['segmentation'] = [
                    p for p in obj['segmentation'] if len(p) >= 6
                ]
            # if obj['area'] < cfg.TRAIN.GT_MIN_AREA:
            if obj['area'] < config.train.gt_min_area:
                continue
            if 'ignore' in obj and obj['ignore'] == 1:
                continue
            # Convert form (x1, y1, w, h) to (x1, y1, x2, y2)
            x1, y1, x2, y2 = box_utils.xywh_to_xyxy(obj['bbox'])
            x1, y1, x2, y2 = box_utils.clip_xyxy_to_image(
                x1, y1, x2, y2, height, width
            )
            # Require non-zero seg area and more than 1x1 box size
            if obj['area'] > 0 and x2 > x1 and y2 > y1:
                obj['clean_bbox'] = [x1, y1, x2, y2]
                valid_objs.append(obj)
                valid_segms.append(obj['segmentation'])
        num_valid_objs = len(valid_objs)

        boxes = np.zeros((num_valid_objs, 4), dtype=entry['boxes'].dtype)
        gt_classes = np.zeros((num_valid_objs,), dtype=entry['gt_classes'].dtype)
        gt_overlaps = np.zeros(
            (num_valid_objs, self.num_classes),
            dtype=entry['gt_overlaps'].dtype
        )
        seg_areas = np.zeros((num_valid_objs,), dtype=entry['seg_areas'].dtype)
        is_crowd = np.zeros((num_valid_objs,), dtype=entry['is_crowd'].dtype)
        box_to_gt_ind_map = np.zeros(
            (num_valid_objs,), dtype=entry['box_to_gt_ind_map'].dtype
        )
        if self.keypoints is not None:
            gt_keypoints = np.zeros(
                (num_valid_objs, 3, self.num_keypoints),
                dtype=entry['gt_keypoints'].dtype
            )

        im_has_visible_keypoints = False
        for ix, obj in enumerate(valid_objs):
            cls = self.json_category_id_to_contiguous_id[obj['category_id']]
            boxes[ix, :] = obj['clean_bbox']
            gt_classes[ix] = cls
            seg_areas[ix] = obj['area']
            is_crowd[ix] = obj['iscrowd']
            box_to_gt_ind_map[ix] = ix
            if self.keypoints is not None:
                gt_keypoints[ix, :, :] = self._get_gt_keypoints(obj)
                if np.sum(gt_keypoints[ix, 2, :]) > 0:
                    im_has_visible_keypoints = True
            if obj['iscrowd']:
                # Set overlap to -1 for all classes for crowd objects
                # so they will be excluded during training
                gt_overlaps[ix, :] = -1.0
            else:
                gt_overlaps[ix, cls] = 1.0

        matched_segms = self.match_ins_pan_ins(boxes, pan_valid_bboxes, pan_valid_segms)
        entry['boxes'] = np.append(entry['boxes'], boxes, axis=0)
        entry['segms'].extend(valid_segms)
        entry['matched_segms'].extend(matched_segms)
        # To match the original implementation:
        # entry['boxes'] = np.append(
        #     entry['boxes'], boxes.astype(np.int).astype(np.float), axis=0)
        entry['gt_classes'] = np.append(entry['gt_classes'], gt_classes)
        entry['seg_areas'] = np.append(entry['seg_areas'], seg_areas)
        entry['gt_overlaps'] = np.append(
            entry['gt_overlaps'].toarray(), gt_overlaps, axis=0
        )
        entry['gt_overlaps'] = scipy.sparse.csr_matrix(entry['gt_overlaps'])
        entry['is_crowd'] = np.append(entry['is_crowd'], is_crowd)
        entry['box_to_gt_ind_map'] = np.append(
            entry['box_to_gt_ind_map'], box_to_gt_ind_map
        )
        if self.keypoints is not None:
            entry['gt_keypoints'] = np.append(
                entry['gt_keypoints'], gt_keypoints, axis=0
            )
            entry['has_visible_keypoints'] = im_has_visible_keypoints

    def _add_proposals_from_file(
        self, roidb, proposal_file, min_proposal_size, top_k, crowd_thresh
    ):
        """Add proposals from a proposals file to an roidb."""
        if logger:
            logger.info('Loading proposals from: {}'.format(proposal_file))
        with open(proposal_file, 'rb') as f:
            proposals = pickle.load(f)
        id_field = 'indexes' if 'indexes' in proposals else 'ids'  # compat fix
        _sort_proposals(proposals, id_field)
        box_list = []
        for i, entry in enumerate(roidb):
            if i % 2500 == 0:
                if logger:
                    logger.info(' {:d}/{:d}'.format(i + 1, len(roidb)))
            boxes = proposals['boxes'][i]
            # Sanity check that these boxes are for the correct image id
            assert entry['id'] == proposals[id_field][i]
            # Remove duplicate boxes and very small boxes and then take top k
            boxes = box_utils.clip_boxes_to_image(
                boxes, entry['height'], entry['width']
            )
            keep = box_utils.unique_boxes(boxes)
            boxes = boxes[keep, :]
            keep = box_utils.filter_boxes(boxes, min_proposal_size)
            boxes = boxes[keep, :]
            if top_k > 0:
                boxes = boxes[:top_k, :]
            box_list.append(boxes)
        _merge_proposal_boxes_into_roidb(roidb, box_list)
        if crowd_thresh > 0:
            _filter_crowd_proposals(roidb, crowd_thresh)

    def _init_keypoints(self):
        """Initialize COCO keypoint information."""
        self.keypoints = None
        self.keypoint_flip_map = None
        self.keypoints_to_id_map = None
        self.num_keypoints = 0
        # Thus far only the 'person' category has keypoints
        if 'person' in self.category_to_id_map:
            cat_info = self.COCO.loadCats([self.category_to_id_map['person']])
        else:
            return

        # Check if the annotations contain keypoint data or not
        if 'keypoints' in cat_info[0]:
            keypoints = cat_info[0]['keypoints']
            self.keypoints_to_id_map = dict(
                zip(keypoints, range(len(keypoints))))
            self.keypoints = keypoints
            self.num_keypoints = len(keypoints)
            self.keypoint_flip_map = {
                'left_eye': 'right_eye',
                'left_ear': 'right_ear',
                'left_shoulder': 'right_shoulder',
                'left_elbow': 'right_elbow',
                'left_wrist': 'right_wrist',
                'left_hip': 'right_hip',
                'left_knee': 'right_knee',
                'left_ankle': 'right_ankle'}

    def _get_gt_keypoints(self, obj):
        """Return ground truth keypoints."""
        if 'keypoints' not in obj:
            return None
        kp = np.array(obj['keypoints'])
        x = kp[0::3]  # 0-indexed x coordinates
        y = kp[1::3]  # 0-indexed y coordinates
        # 0: not labeled; 1: labeled, not inside mask;
        # 2: labeled and inside mask
        v = kp[2::3]
        num_keypoints = len(obj['keypoints']) / 3
        assert num_keypoints == self.num_keypoints
        gt_kps = np.ones((3, self.num_keypoints), dtype=np.int32)
        for i in range(self.num_keypoints):
            gt_kps[0, i] = x[i]
            gt_kps[1, i] = y[i]
            gt_kps[2, i] = v[i]
        return gt_kps


def add_proposals(roidb, rois, scales, crowd_thresh):
    """Add proposal boxes (rois) to an roidb that has ground-truth annotations
    but no proposals. If the proposals are not at the original image scale,
    specify the scale factor that separate them in scales.
    """
    box_list = []
    for i in range(len(roidb)):
        inv_im_scale = 1. / scales[i]
        idx = np.where(rois[:, 0] == i)[0]
        box_list.append(rois[idx, 1:] * inv_im_scale)
    _merge_proposal_boxes_into_roidb(roidb, box_list)
    if crowd_thresh > 0:
        _filter_crowd_proposals(roidb, crowd_thresh)
    _add_class_assignments(roidb)


def extend_with_flipped_entries(roidb, dataset):
    """Flip each entry in the given roidb and return a new roidb that is the
    concatenation of the original roidb and the flipped entries.
    "Flipping" an entry means that that image and associated metadata (e.g.,
    ground truth boxes and object proposals) are horizontally flipped.
    """
    flipped_roidb = []
    for entry in roidb:
        width = entry['width']
        boxes = entry['boxes'].copy()
        oldx1 = boxes[:, 0].copy()
        oldx2 = boxes[:, 2].copy()
        boxes[:, 0] = width - oldx2 - 1
        boxes[:, 2] = width - oldx1 - 1
        assert (boxes[:, 2] >= boxes[:, 0]).all()
        flipped_entry = {}
        dont_copy = ('boxes', 'segms', 'matched_segms', 'gt_keypoints', 'flipped')
        for k, v in entry.items():
            if k not in dont_copy:
                flipped_entry[k] = v
        flipped_entry['boxes'] = boxes
        flipped_entry['segms'] = segm_utils.flip_segms(
            entry['segms'], entry['height'], entry['width']
        )
        flipped_entry['matched_segms'] = segm_utils.flip_segms(
            entry['matched_segms'], entry['height'], entry['width']
        )

        # if dataset.keypoints is not None:
        #     flipped_entry['gt_keypoints'] = keypoint_utils.flip_keypoints(
        #         dataset.keypoints, dataset.keypoint_flip_map,
        #         entry['gt_keypoints'], entry['width']
        #     )
        flipped_entry['flipped'] = True
        flipped_roidb.append(flipped_entry)
    roidb.extend(flipped_roidb)


def filter_for_training(roidb):
    """Remove roidb entries that have no usable RoIs based on config settings.
    """
    def is_valid(entry):
        # Valid images have:
        #   (1) At least one foreground RoI OR
        #   (2) At least one background RoI
        overlaps = entry['max_overlaps']
        # find boxes with sufficient overlap
        fg_inds = np.where(overlaps >= config.train.fg_thresh)[0]
        # Select background RoIs as those within [BG_THRESH_LO, BG_THRESH_HI)
        bg_inds = np.where((overlaps < config.train.bg_thresh_hi) &
                           (overlaps >= config.train.bg_thresh_lo))[0]
        # image is only valid if such boxes exist
        valid = len(fg_inds) > 0 or len(bg_inds) > 0
        # if cfg.MODEL.KEYPOINTS_ON:
        #     # If we're training for keypoints, exclude images with no keypoints
        #     valid = valid and entry['has_visible_keypoints']
        return valid

    num = len(roidb)
    filtered_roidb = [entry for entry in roidb if is_valid(entry)]
    num_after = len(filtered_roidb)
    if logger:
        logger.info('Filtered {} roidb entries: {} -> {}'.
               format(num - num_after, num, num_after))
    return filtered_roidb


def add_bbox_regression_targets(roidb):
    """Add information needed to train bounding-box regressors."""
    for entry in roidb:
        entry['bbox_targets'] = _compute_targets(entry)


def _compute_targets(entry):
    """Compute bounding-box regression targets for an image."""
    # Indices of ground-truth ROIs
    rois = entry['boxes']
    overlaps = entry['max_overlaps']
    labels = entry['max_classes']
    gt_inds = np.where((entry['gt_classes'] > 0) & (entry['is_crowd'] == 0))[0]
    # Targets has format (class, tx, ty, tw, th)
    targets = np.zeros((rois.shape[0], 5), dtype=np.float32)
    if len(gt_inds) == 0:
        # Bail if the image has no ground-truth ROIs
        return targets

    # Indices of examples for which we try to make predictions
    ex_inds = np.where(overlaps >= config.train.bbox_thresh)[0]

    # Get IoU overlap between each ex ROI and gt ROI
    ex_gt_overlaps = box_utils.bbox_overlaps(
        rois[ex_inds, :].astype(dtype=np.float32, copy=False),
        rois[gt_inds, :].astype(dtype=np.float32, copy=False))

    # Find which gt ROI each ex ROI has max overlap with:
    # this will be the ex ROI's gt target
    gt_assignment = ex_gt_overlaps.argmax(axis=1)
    gt_rois = rois[gt_inds[gt_assignment], :]
    ex_rois = rois[ex_inds, :]
    # Use class "1" for all boxes if using class_agnostic_bbox_reg
    targets[ex_inds, 0] = (
        1 if config.network.cls_agnostic_bbox_reg else labels[ex_inds])
    targets[ex_inds, 1:] = box_utils.bbox_transform_inv(
        ex_rois, gt_rois, config.network.bbox_reg_weights)
    return targets


def _merge_proposal_boxes_into_roidb(roidb, box_list):
    """Add proposal boxes to each roidb entry."""
    assert len(box_list) == len(roidb)
    for i, entry in enumerate(roidb):
        boxes = box_list[i]
        num_boxes = boxes.shape[0]
        gt_overlaps = np.zeros(
            (num_boxes, entry['gt_overlaps'].shape[1]),
            dtype=entry['gt_overlaps'].dtype
        )
        box_to_gt_ind_map = -np.ones(
            (num_boxes,), dtype=entry['box_to_gt_ind_map'].dtype
        )

        # Note: unlike in other places, here we intentionally include all gt
        # rois, even ones marked as crowd. Boxes that overlap with crowds will
        # be filtered out later (see: _filter_crowd_proposals).
        gt_inds = np.where(entry['gt_classes'] > 0)[0]
        if len(gt_inds) > 0:
            gt_boxes = entry['boxes'][gt_inds, :]
            gt_classes = entry['gt_classes'][gt_inds]
            proposal_to_gt_overlaps = box_utils.bbox_overlaps(
                boxes.astype(dtype=np.float32, copy=False),
                gt_boxes.astype(dtype=np.float32, copy=False)
            )
            # Gt box that overlaps each input box the most
            # (ties are broken arbitrarily by class order)
            argmaxes = proposal_to_gt_overlaps.argmax(axis=1)
            # Amount of that overlap
            maxes = proposal_to_gt_overlaps.max(axis=1)
            # Those boxes with non-zero overlap with gt boxes
            I = np.where(maxes > 0)[0]
            # Record max overlaps with the class of the appropriate gt box
            gt_overlaps[I, gt_classes[argmaxes[I]]] = maxes[I]
            box_to_gt_ind_map[I] = gt_inds[argmaxes[I]]
        entry['boxes'] = np.append(
            entry['boxes'],
            boxes.astype(entry['boxes'].dtype, copy=False),
            axis=0
        )
        entry['gt_classes'] = np.append(
            entry['gt_classes'],
            np.zeros((num_boxes,), dtype=entry['gt_classes'].dtype)
        )
        entry['seg_areas'] = np.append(
            entry['seg_areas'],
            np.zeros((num_boxes,), dtype=entry['seg_areas'].dtype)
        )
        entry['gt_overlaps'] = np.append(
            entry['gt_overlaps'].toarray(), gt_overlaps, axis=0
        )
        entry['gt_overlaps'] = scipy.sparse.csr_matrix(entry['gt_overlaps'])
        entry['is_crowd'] = np.append(
            entry['is_crowd'],
            np.zeros((num_boxes,), dtype=entry['is_crowd'].dtype)
        )
        entry['box_to_gt_ind_map'] = np.append(
            entry['box_to_gt_ind_map'],
            box_to_gt_ind_map.astype(
                entry['box_to_gt_ind_map'].dtype, copy=False
            )
        )


def _filter_crowd_proposals(roidb, crowd_thresh):
    """Finds proposals that are inside crowd regions and marks them as
    overlap = -1 with each ground-truth rois, which means they will be excluded
    from training.
    """
    for entry in roidb:
        gt_overlaps = entry['gt_overlaps'].toarray()
        crowd_inds = np.where(entry['is_crowd'] == 1)[0]
        non_gt_inds = np.where(entry['gt_classes'] == 0)[0]
        if len(crowd_inds) == 0 or len(non_gt_inds) == 0:
            continue
        crowd_boxes = box_utils.xyxy_to_xywh(entry['boxes'][crowd_inds, :])
        non_gt_boxes = box_utils.xyxy_to_xywh(entry['boxes'][non_gt_inds, :])
        iscrowd_flags = [int(True)] * len(crowd_inds)
        ious = COCOmask.iou(non_gt_boxes, crowd_boxes, iscrowd_flags)
        bad_inds = np.where(ious.max(axis=1) > crowd_thresh)[0]
        gt_overlaps[non_gt_inds[bad_inds], :] = -1
        entry['gt_overlaps'] = scipy.sparse.csr_matrix(gt_overlaps)


def _add_class_assignments(roidb):
    """Compute object category assignment for each box associated with each
    roidb entry.
    """
    for entry in roidb:
        gt_overlaps = entry['gt_overlaps'].toarray()
        # max overlap with gt over classes (columns)
        max_overlaps = gt_overlaps.max(axis=1)
        # gt class that had the max overlap
        max_classes = gt_overlaps.argmax(axis=1)
        entry['max_classes'] = max_classes
        entry['max_overlaps'] = max_overlaps
        # sanity checks
        # if max overlap is 0, the class must be background (class 0)
        zero_inds = np.where(max_overlaps == 0)[0]
        assert all(max_classes[zero_inds] == 0)
        # if max overlap > 0, the class must be a fg class (not class 0)
        nonzero_inds = np.where(max_overlaps > 0)[0]
        assert all(max_classes[nonzero_inds] != 0)


def _sort_proposals(proposals, id_field):
    """Sort proposals by the specified id field."""
    order = np.argsort(proposals[id_field])
    fields_to_sort = ['boxes', id_field, 'scores']
    for k in fields_to_sort:
        proposals[k] = [proposals[k][i] for i in order]
