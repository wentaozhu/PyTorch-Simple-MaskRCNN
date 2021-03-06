from concurrent.futures import ThreadPoolExecutor, as_completed

import torch
import torch.nn.functional as F
try:
    import matplotlib.pyplot as plt
except ImportError:
    pass

from .box_ops import box_iou


def factor_getter(n, base):
    base = base * 0.8 ** (n // 6)
    i = n % 6
    if i < 3:
        f = [0, 0, 0]
        f[i] = base
    else:
        base /= 2
        f = [base, base, base]
        f[i - 3] = 0
    return f


def resize(image, target, scale_factor):
    ori_image_shape = image.shape[-2:]
    image = F.interpolate(image[None], scale_factor=scale_factor, mode='bilinear', align_corners=False)[0]

    if target is None:
        return image, target

    if 'boxes' in target:
        box = target['boxes']
        box[:, [0, 2]] = box[:, [0, 2]] * image.shape[-1] / ori_image_shape[1]
        box[:, [1, 3]] = box[:, [1, 3]] * image.shape[-2] / ori_image_shape[0]
        target['boxes'] = box

    if 'masks' in target:
        mask = target['masks']
        mask = F.interpolate(mask[None].float(), scale_factor=scale_factor)[0].byte()
        target['masks'] = mask

    return image, target
    

def show(image, target=None, classes=None, scale_factor=None, base=0.4):
    image = image.clone()
    
    if scale_factor is not None:
        image, target = resize(image, target, scale_factor)
        
    if target is not None and 'masks' in target:
        mask = target['masks']
        mask = mask.reshape(-1, 1, mask.shape[1], mask.shape[2])
        mask = mask.repeat(1, 3, 1, 1).to(image)
        for i, m in enumerate(mask):
            factor = torch.tensor(factor_getter(i, base)).reshape(3, 1, 1).to(image)
            value = factor * m
            image += value
        
    image = image.clamp(0, 1)
    im = image.cpu().numpy()
    plt.imshow(im.transpose(1, 2, 0))
    
    if target is not None:
        if 'boxes' in target:
            box = target['boxes']
            box = box.cpu()
            for i, b in enumerate(box):
                plt.plot(b[[0, 2, 2, 0, 0]], b[[1, 1, 3, 3, 1]])
                if 'labels' in target:
                    l = target['labels'][i].item()
                    if classes is None:
                        raise ValueError("'classes' should not be None when 'target' has 'labels'!")
                    txt = classes[l]
                    if 'scores' in target:
                        s = target['scores'][i]
                        s = round(s.item() * 100)
                        txt = '{} {}%'.format(txt, s)
                    plt.text(
                        b[0], b[1], txt, fontsize=14, 
                        bbox=dict(boxstyle='round', fc='white', lw=1, alpha=0.7))
            
    plt.title(im.shape)
    plt.axis('off')
    plt.show()
    
    
class APGetter:
    def __init__(self, num_classes, device='cpu', max_workers=1):
        self.container = {i:{'score':torch.tensor([], device=device),
                                  'iou':torch.tensor([], device=device)} for i in range(num_classes)}
        
        self.thresholds = [i / 100 for i in range(50, 96, 5)]
        self.AP_series = torch.zeros((len(self.thresholds), num_classes), device=device)
        self.mAP = None
        self.max_workers = max_workers
        
    def collect_data(self, result, target):
        box, score, label = result['boxes'], result['scores'], result['labels']
        gt_box, gt_label = target['boxes'], target['labels']

        current_class = gt_label.unique()
        for cls in current_class:
            cls_box, cls_score = box[label == cls], score[label == cls]
            gt_cls_box = gt_box[gt_label == cls]

            if len(cls_box) == 0:
                cls_score = cls_score.new_zeros(len(gt_cls_box))
                cls_box_repr_iou = cls_box.new_zeros(len(gt_cls_box))
            else:
                cls_box_repr_iou = cls_box.new_full((len(cls_box),), -1)
                iou = box_iou(gt_cls_box, cls_box)
                value, idx = iou.max(dim=0)

                for i in range(len(gt_cls_box)):
                    matched_idx = torch.where(idx == i)[0]

                    if len(matched_idx) == 0:
                        cls_score = torch.cat((cls_score, cls_score.new_zeros(1)), dim=0) # 0 is critical, maybe it's 1?
                        cls_box_repr_iou = torch.cat((cls_box_repr_iou, cls_box_repr_iou.new_zeros(1)), dim=0)
                    else:
                        matched_value = value[matched_idx]
                        repr_iou, rel_idx = matched_value.max(dim=0)
                        cls_box_repr_iou[matched_idx[rel_idx]] = repr_iou

            key = cls.item()
            pred_score = self.container[key]['score']
            pred_score = torch.cat((pred_score, cls_score), dim=0)
            self.container[key]['score'] = pred_score

            pred_iou = self.container[key]['iou']
            pred_iou = torch.cat((pred_iou, cls_box_repr_iou), dim=0)
            self.container[key]['iou'] = pred_iou
            

    def _ap_single_class(self, t, k):
        threshold = self.thresholds[t]
        score, iou = self.container[k]['score'], self.container[k]['iou']
        
        order = score.sort(descending=True)[1]
        score, iou = score[order], iou[order]
        label = (iou >= threshold).to(score)

        sample_target = torch.where(iou >= 0)[0]

        if len(sample_target) == 0:
            self.AP_series[t, k] = 0.
            return
        else:
            precision = score.new_zeros(len(sample_target))

        TP = score.new_tensor(0)
        for i, n in enumerate(sample_target):
            TP += label[n]
            precision[i] = TP / (n + 1)
            
        self.AP_series[t, k] = precision.mean()
    
    def compute_ap(self):
        executor = ThreadPoolExecutor(max_workers=self.max_workers)
        tasks = []
        for t in range(len(self.thresholds)):
            for k in self.container:
                tasks.append(executor.submit(self._ap_single_class, t, k))

        for future in as_completed(tasks):
            pass
        
        self.AP_series = self.AP_series.mean(dim=1).tolist()
        self.mAP = torch.tensor(self.AP_series).mean().item()


def generate_bbox(num, size, manual_seed=True):
    if manual_seed:
        torch.manual_seed(3)
    x = torch.randint(0, size[1], (2, num), dtype=torch.float32)
    y = torch.randint(0, size[0], (2, num), dtype=torch.float32)
    
    ymin = y.min(dim=0)[0]
    xmin = x.min(dim=0)[0]
    ymax = y.max(dim=0)[0] + 1
    xmax = x.max(dim=0)[0] + 1
    
    bbox = torch.stack((xmin, ymin, xmax, ymax), dim=1)
    return bbox


class Meter:
    def __init__(self, name):
        self.name = name
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name}:sum={sum:.2f}, avg={avg:.2f}, count={count}'
        return fmtstr.format(**self.__dict__)