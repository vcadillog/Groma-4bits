import random
import torch
import numpy as np
from mmcv.ops.nms import nms
from mmdet.datasets import CocoDataset
from mmdet.datasets.api_wrappers import COCO
from mmdet.core.bbox.transforms import bbox_xyxy_to_cxcywh

from groma.data.datasets.flickr import INSTRUCTIONS
from groma.data.datasets.det_data import normalize_box_coordinates
from groma.constants import DEFAULT_TOKENS, IGNORE_INDEX
from groma.data.conversation import conv_templates


INSTRUCTIONS = [
    "What is {}?",
    "Please briefly describe {}.",
    "Provide a short description for {}.",
    "Please give a concise description of region {}."
]


class SingleRoundVG(CocoDataset):
    CLASSES = ('object',)

    def __init__(
        self,
        ann_file=None,
        img_prefix=None,
        tokenizer=None,
        test_mode=False,
        conv_temp='default'
    ):
        self.tokenizer = tokenizer
        self.conv_temp = conv_templates[conv_temp]
        self.seperator_id = self.tokenizer.convert_tokens_to_ids([DEFAULT_TOKENS['sep']])[0]
        self.eos_id = self.tokenizer.convert_tokens_to_ids([DEFAULT_TOKENS['eos']])[0]

        img_norm_cfg = dict(
            mean=[0.485 * 255, 0.456 * 255, 0.406 * 255],
            std=[0.229 * 255, 0.224 * 255, 0.225 * 255],
            to_rgb=True
        )

        train_pipeline = [
            dict(type='LoadImageFromFile'),
            dict(type='LoadAnnotations', with_bbox=True),
            dict(type='Resize', img_scale=(448, 448), keep_ratio=False),
            dict(type='FilterAnnotationsFlickr', min_gt_bbox_wh=(2.0, 2.0)),
            dict(type='RandomFlip', flip_ratio=0.),
            dict(type='Normalize', **img_norm_cfg),
            dict(type='Pad', size_divisor=448),
            dict(type='DefaultFormatBundleFlickr'),
            dict(type='Collect', keys=['img', 'gt_bboxes', 'gt_labels']),
        ]

        test_pipeline = [
            dict(type='LoadImageFromFile'),
            dict(type='LoadAnnotations', with_bbox=True),
            dict(type='Resize', img_scale=(448, 448), keep_ratio=False),
            dict(type='FilterAnnotationsFlickr', min_gt_bbox_wh=(2.0, 2.0)),
            dict(type='RandomFlip', flip_ratio=0.),
            dict(type='Normalize', **img_norm_cfg),
            dict(type='Pad', size_divisor=448),
            dict(type='DefaultFormatBundleFlickr'),
            dict(type='Collect', keys=['img', 'gt_bboxes', 'gt_labels', 'img_info']),
        ]

        pipeline = test_pipeline if test_mode else train_pipeline
        dataset_cfg = dict(
            ann_file=ann_file,
            img_prefix=img_prefix,
            test_mode=False,
            pipeline=pipeline)

        super(CocoDataset, self).__init__(**dataset_cfg)

    def _filter_imgs(self, min_size=32):
        """Filter images too small or without ground truths."""
        valid_inds = []
        # TODO: obtain images that contain annotation
        valid_img_ids = []
        for i, img_info in enumerate(self.data_infos):
            img_id = self.img_ids[i]
            if min(img_info['width'], img_info['height']) >= min_size:
                valid_inds.append(i)
                valid_img_ids.append(img_id)
        self.img_ids = valid_img_ids
        return valid_inds

    def load_annotations(self, ann_file):
        """Load annotation from COCO style annotation file.

        Args:
            ann_file (str): Path of annotation file.

        Returns:
            list[dict]: Annotation info from COCO api.
        """

        self.coco = COCO(ann_file)
        # The order of returned `cat_ids` will not
        # change with the order of the CLASSES
        self.cat_ids = self.coco.get_cat_ids(cat_names=self.CLASSES)

        self.cat2label = {cat_id: i for i, cat_id in enumerate(self.cat_ids)}
        self.img_ids = self.coco.get_img_ids()
        data_infos = []
        total_ann_ids = []
        for i in self.img_ids:
            info = self.coco.load_imgs([i])[0]
            info['filename'] = info['file_name']
            # convert data type for flickr
            info['height'] = int(info['height'])
            info['width'] = int(info['width'])
            ann_ids = self.coco.get_ann_ids(img_ids=[i])
            if len(ann_ids) == 0:
                continue
            data_infos.append(info)
            total_ann_ids.extend(ann_ids)
        assert len(set(total_ann_ids)) == len(
            total_ann_ids), f"Annotation ids in '{ann_file}' are not unique!"
        return data_infos

    def _parse_ann_info(self, img_info, ann_info):
        """Parse bbox and mask annotation.

        Args:
            ann_info (list[dict]): Annotation info of an image.
            with_mask (bool): Whether to parse mask annotations.

        Returns:
            dict: A dict containing the following keys: bboxes, bboxes_ignore,\
                labels, masks, seg_map. "masks" are raw annotations and not \
                decoded into binary masks.
        """
        gt_bboxes = []
        gt_labels = []
        gt_bboxes_ignore = []
        gt_masks_ann = []

        # flickr
        for i, ann in enumerate(ann_info):
            if ann.get('ignore', False):
                continue
            x1, y1, w, h = ann['bbox']
            inter_w = max(0, min(x1 + w, img_info['width']) - max(x1, 0))
            inter_h = max(0, min(y1 + h, img_info['height']) - max(y1, 0))
            if inter_w * inter_h == 0:
                continue
            if ann['area'] <= 0 or w < 1 or h < 1:
                continue
            if ann['category_id'] not in self.cat_ids:
                continue
            bbox = [x1, y1, x1 + w, y1 + h]
            if bbox not in gt_bboxes:
                gt_bboxes.append(bbox)
                gt_labels.append(ann['caption'])
                gt_masks_ann.append(ann.get('segmentation', None))

        if gt_bboxes:
            gt_bboxes = np.array(gt_bboxes, dtype=np.float32)
        else:
            gt_bboxes = np.zeros((0, 4), dtype=np.float32)

        if gt_bboxes_ignore:
            gt_bboxes_ignore = np.array(gt_bboxes_ignore, dtype=np.float32)
        else:
            gt_bboxes_ignore = np.zeros((0, 4), dtype=np.float32)

        seg_map = img_info['filename'].replace('jpg', 'png')

        ann = dict(
            bboxes=gt_bboxes,
            labels=gt_labels,
            bboxes_ignore=gt_bboxes_ignore,
            masks=gt_masks_ann,
            seg_map=seg_map)
        return ann

    def preprocess(self, data_item):
        image = data_item['img'].data
        label = data_item['gt_labels'][0]
        bboxes = data_item['gt_bboxes'].data
        img_shape = data_item['img_metas'].data['img_shape']
        bboxes = bbox_xyxy_to_cxcywh(bboxes)
        bboxes = normalize_box_coordinates(bboxes, img_shape)

        conversations = []
        instruct = "Here is an image with region crops from it. "
        instruct += "Image: {}. ".format(DEFAULT_TOKENS['image'])
        instruct += "Regions: {}.".format(DEFAULT_TOKENS['region'])
        answer = 'Thank you for the image! How can I assist you with it?'
        conversations.append((self.conv_temp.roles[0], instruct))
        conversations.append((self.conv_temp.roles[1], answer))

        refer_exp = DEFAULT_TOKENS['bor'] + DEFAULT_TOKENS['rbox'] + DEFAULT_TOKENS['eor']
        refer_exp += DEFAULT_TOKENS['rfeat']
        instruct = random.choice(INSTRUCTIONS).format(refer_exp)
        answer = DEFAULT_TOKENS['sep'] + label.strip().lower().capitalize() + DEFAULT_TOKENS['sep']
        conversations.append((self.conv_temp.roles[0], instruct))
        conversations.append((self.conv_temp.roles[1], answer))
        prompt = self.conv_temp.get_prompt(conversations)

        # tokenize conversations
        input_ids = self.tokenizer(
            prompt,
            return_tensors="pt",
            padding="longest",
            max_length=self.tokenizer.model_max_length,
            truncation=True
        ).input_ids[0]

        # Mask targets
        targets = input_ids.clone()
        sep_inds = (input_ids == self.seperator_id).nonzero(as_tuple=True)[0]
        assert len(sep_inds) % 2 == 0
        for i in range(0, len(sep_inds), 2):
            pre_sep = 0 if i == 0 else sep_inds[i - 1]
            cur_sep = sep_inds[i]
            targets[pre_sep:cur_sep] = IGNORE_INDEX
        eos_inds = (input_ids == self.eos_id).nonzero(as_tuple=True)[0]
        targets[eos_inds[1:]] = self.eos_id

        # Remove sep token
        mask = input_ids != self.seperator_id
        input_ids = input_ids[mask]
        targets = targets[mask]

        data_dict = dict(
            input_ids=input_ids,
            labels=targets,
            image=image,
            source='visual_genome',
            refer_boxes=bboxes,
            img_metas=data_item['img_metas'].data
        )
        return data_dict

    def __getitem__(self, idx):
        data_item = super().__getitem__(idx)
        data_dict = self.preprocess(data_item)
        return data_dict


class MultiRoundsVG(SingleRoundVG):
    def __init__(
        self,
        ann_file=None,
        img_prefix=None,
        tokenizer=None,
        test_mode=False,
        conv_temp='default'
    ):
        self.max_gt_per_img = 10
        super().__init__(
            ann_file=ann_file,
            img_prefix=img_prefix,
            tokenizer=tokenizer,
            test_mode=test_mode,
            conv_temp=conv_temp
        )

    def preprocess(self, data_item):
        image = data_item['img'].data
        labels = data_item['gt_labels']
        bboxes = data_item['gt_bboxes'].data

        rand_scores = torch.rand(len(bboxes))
        nms_inds = nms(bboxes, rand_scores, 0.6)[-1]
        labels = [labels[i] for i in nms_inds]
        bboxes = bboxes[nms_inds]

        img_shape = data_item['img_metas'].data['img_shape']
        bboxes = bbox_xyxy_to_cxcywh(bboxes)
        bboxes = normalize_box_coordinates(bboxes, img_shape)

        if len(labels) > self.max_gt_per_img:
            bboxes = bboxes[:self.max_gt_per_img]
            labels = labels[:self.max_gt_per_img]

        conversations = []
        instruct = "Here is an image with region crops from it. "
        instruct += "Image: {}. ".format(DEFAULT_TOKENS['image'])
        instruct += "Regions: {}.".format(DEFAULT_TOKENS['region'])
        answer = 'Thank you for the image! How can I assist you with it?'
        conversations.append((self.conv_temp.roles[0], instruct))
        conversations.append((self.conv_temp.roles[1], answer))
        for label in labels:
            refer_exp = DEFAULT_TOKENS['bor'] + DEFAULT_TOKENS['rbox'] + DEFAULT_TOKENS['eor']
            refer_exp += DEFAULT_TOKENS['rfeat']
            instruct = random.choice(INSTRUCTIONS).format(refer_exp)
            answer = DEFAULT_TOKENS['sep'] + label.strip().lower().capitalize() + DEFAULT_TOKENS['sep']
            conversations.append((self.conv_temp.roles[0], instruct))
            conversations.append((self.conv_temp.roles[1], answer))
        prompt = self.conv_temp.get_prompt(conversations)

        # tokenize conversations
        input_ids = self.tokenizer(
            prompt,
            return_tensors="pt",
            padding="longest",
            max_length=self.tokenizer.model_max_length,
            truncation=True
        ).input_ids[0]

        # Mask targets
        targets = input_ids.clone()
        sep_inds = (input_ids == self.seperator_id).nonzero(as_tuple=True)[0]
        assert len(sep_inds) % 2 == 0
        for i in range(0, len(sep_inds), 2):
            pre_sep = 0 if i == 0 else sep_inds[i-1]
            cur_sep = sep_inds[i]
            targets[pre_sep:cur_sep] = IGNORE_INDEX
        eos_inds = (input_ids == self.eos_id).nonzero(as_tuple=True)[0]
        targets[eos_inds[1:]] = self.eos_id

        # Remove sep token
        mask = input_ids != self.seperator_id
        input_ids = input_ids[mask]
        targets = targets[mask]

        data_dict = dict(
            input_ids=input_ids,
            labels=targets,
            image=image,
            source='visual_genome',
            refer_boxes=bboxes,
            img_metas=data_item['img_metas'].data
        )
        return data_dict
