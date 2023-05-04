import datetime
import os
import time
import tracemalloc

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from tensorboardX import SummaryWriter
from torch.utils.data import DataLoader

from models.afwm_pb import TVLoss
from models.afwm_pb import AFWM as PBAFWM
from models.networks import ResUnetGenerator, VGGLoss
from options.train_options import TrainOptions
from utils.utils import load_checkpoint_parallel, save_checkpoint
from data.dresscode_dataset import DressCodeDataset
from data.viton_dataset import LoadVITONDataset


def run_once(
    opt, data, step, device, model, model_gen, TVLoss, criterionL1, criterionVGG, writer
):
    t_mask = torch.FloatTensor((data["label"].cpu().numpy() == 7).astype(np.float64))
    data["label"] = data["label"] * (1 - t_mask) + t_mask * 4
    edge = data["edge"]
    pre_clothes_edge = torch.FloatTensor((edge.detach().numpy() > 0.5).astype(np.int64))
    clothes = data["color"]
    clothes = clothes * pre_clothes_edge
    person_clothes_edge = torch.FloatTensor(
        (data["label"].cpu().numpy() == 4).astype(np.int64)
    )
    real_image = data["image"]
    person_clothes = real_image * person_clothes_edge
    pose = data["pose"]
    size = data["label"].size()
    oneHot_size1 = (size[0], 25, size[2], size[3])
    densepose = torch.cuda.FloatTensor(torch.Size(oneHot_size1)).zero_()
    densepose = densepose.scatter_(1, data["densepose"].data.long().to(device), 1.0)
    densepose_fore = data["densepose"] / 24.0
    face_mask = torch.FloatTensor(
        (data["label"].cpu().numpy() == 1).astype(np.int64)
    ) + torch.FloatTensor((data["label"].cpu().numpy() == 12).astype(np.int64))
    other_clothes_mask = (
        torch.FloatTensor((data["label"].cpu().numpy() == 5).astype(np.int64))
        + torch.FloatTensor((data["label"].cpu().numpy() == 6).astype(np.int64))
        + torch.FloatTensor((data["label"].cpu().numpy() == 8).astype(np.int64))
        + torch.FloatTensor((data["label"].cpu().numpy() == 9).astype(np.int64))
        + torch.FloatTensor((data["label"].cpu().numpy() == 10).astype(np.int64))
    )
    face_img = face_mask * real_image
    other_clothes_img = other_clothes_mask * real_image
    preserve_region = face_img + other_clothes_img
    preserve_mask = torch.cat([face_mask, other_clothes_mask], 1)
    concat = torch.cat([preserve_mask.to(device), densepose, pose.to(device)], 1)
    arm_mask = torch.FloatTensor(
        (data["label"].cpu().numpy() == 11).astype(np.float64)
    ) + torch.FloatTensor((data["label"].cpu().numpy() == 13).astype(np.float64))
    hand_mask = torch.FloatTensor(
        (data["densepose"].cpu().numpy() == 3).astype(np.int64)
    ) + torch.FloatTensor((data["densepose"].cpu().numpy() == 4).astype(np.int64))
    hand_mask = arm_mask * hand_mask
    hand_img = hand_mask * real_image
    dense_preserve_mask = (
        torch.FloatTensor((data["densepose"].cpu().numpy() == 15).astype(np.int64))
        + torch.FloatTensor((data["densepose"].cpu().numpy() == 16).astype(np.int64))
        + torch.FloatTensor((data["densepose"].cpu().numpy() == 17).astype(np.int64))
        + torch.FloatTensor((data["densepose"].cpu().numpy() == 18).astype(np.int64))
        + torch.FloatTensor((data["densepose"].cpu().numpy() == 19).astype(np.int64))
        + torch.FloatTensor((data["densepose"].cpu().numpy() == 20).astype(np.int64))
        + torch.FloatTensor((data["densepose"].cpu().numpy() == 21).astype(np.int64))
        + torch.FloatTensor((data["densepose"].cpu().numpy() == 22))
    )
    dense_preserve_mask = dense_preserve_mask.to(device) * (
        1 - person_clothes_edge.to(device)
    )
    preserve_region = face_img + other_clothes_img + hand_img

    flow_out = model(concat.to(device), clothes.to(device), pre_clothes_edge.to(device))
    (
        warped_cloth,
        last_flow,
        _1,
        _2,
        delta_list,
        x_all,
        x_edge_all,
        delta_x_all,
        delta_y_all,
    ) = flow_out

    epsilon = 0.001
    loss_smooth = sum([TVLoss(x) for x in delta_list])
    warp_loss = 0

    for num in range(5):
        cur_person_clothes = F.interpolate(
            person_clothes, scale_factor=0.5 ** (4 - num), mode="bilinear"
        )
        cur_person_clothes_edge = F.interpolate(
            person_clothes_edge, scale_factor=0.5 ** (4 - num), mode="bilinear"
        )
        loss_l1 = criterionL1(x_all[num], cur_person_clothes.to(device))
        loss_vgg = criterionVGG(x_all[num], cur_person_clothes.to(device))
        loss_edge = criterionL1(x_edge_all[num], cur_person_clothes_edge.to(device))
        b, c, h, w = delta_x_all[num].shape
        loss_flow_x = (delta_x_all[num].pow(2) + epsilon * epsilon).pow(0.45)
        loss_flow_x = torch.sum(loss_flow_x) / (b * c * h * w)
        loss_flow_y = (delta_y_all[num].pow(2) + epsilon * epsilon).pow(0.45)
        loss_flow_y = torch.sum(loss_flow_y) / (b * c * h * w)
        loss_second_smooth = loss_flow_x + loss_flow_y
        warp_loss = (
            warp_loss
            + (num + 1) * loss_l1
            + (num + 1) * 0.2 * loss_vgg
            + (num + 1) * 2 * loss_edge
            + (num + 1) * 6 * loss_second_smooth
        )

    warp_loss = 0.01 * loss_smooth + warp_loss

    warped_prod_edge = x_edge_all[4]
    gen_inputs = torch.cat(
        [
            preserve_region.to(device),
            warped_cloth,
            warped_prod_edge,
            dense_preserve_mask,
        ],
        1,
    )

    gen_outputs = model_gen(gen_inputs)
    p_rendered, m_composite = torch.split(gen_outputs, [3, 1], 1)
    p_rendered = torch.tanh(p_rendered)
    m_composite = torch.sigmoid(m_composite)
    m_composite = m_composite * warped_prod_edge
    # TUNGPNT2
    # m_composite =  person_clothes_edge.to(device)*m_composite
    p_tryon = warped_cloth * m_composite + p_rendered * (1 - m_composite)

    # TUNGPNT2
    # loss_mask_l1 = torch.mean(torch.abs(1 - m_composite))
    # loss_l1 = criterionL1(p_tryon, real_image.to(device))
    # loss_vgg = criterionVGG(p_tryon,real_image.to(device))
    # bg_loss_l1 = criterionL1(p_rendered, real_image.to(device))
    # bg_loss_vgg = criterionVGG(p_rendered, real_image.to(device))
    # gen_loss = (loss_l1 * 5 + loss_vgg + bg_loss_l1 * 5 + bg_loss_vgg + loss_mask_l1)

    loss_mask_l1 = criterionL1(person_clothes_edge.to(device), m_composite)
    loss_l1 = criterionL1(p_tryon, real_image.to(device))
    loss_vgg = criterionVGG(p_tryon, real_image.to(device))
    gen_loss = loss_l1 * 5 + loss_vgg + loss_mask_l1
    loss_all = 0.5 * warp_loss + 1.0 * gen_loss

    path = "sample/" + opt.name
    os.makedirs(path, exist_ok=True)
    if step % 1000 == 0:
        if opt.local_rank == 0:
            a = real_image.float().to(device)
            b = person_clothes.to(device)
            c = clothes.to(device)
            d = torch.cat(
                [
                    densepose_fore.to(device),
                    densepose_fore.to(device),
                    densepose_fore.to(device),
                ],
                1,
            )
            e = warped_cloth
            f = torch.cat([warped_prod_edge, warped_prod_edge, warped_prod_edge], 1)
            g = preserve_region.to(device)
            h = torch.cat(
                [dense_preserve_mask, dense_preserve_mask, dense_preserve_mask], 1
            )
            i = p_rendered
            j = torch.cat([m_composite, m_composite, m_composite], 1)
            k = p_tryon
            combine = torch.cat(
                [a[0], b[0], c[0], d[0], e[0], f[0], g[0], h[0], i[0], j[0], k[0]], 2
            ).squeeze()
            cv_img = (combine.permute(1, 2, 0).detach().cpu().numpy() + 1) / 2
            writer.add_image("combine", (combine.data + 1) / 2.0, step)
            rgb = (cv_img * 255).astype(np.uint8)
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            cv2.imwrite("sample/" + opt.name + "/" + str(step) + ".jpg", bgr)

    return warp_loss, gen_loss, loss_all


opt = TrainOptions().parse()
path = "runs/" + opt.name
os.makedirs(path, exist_ok=True)
os.makedirs(opt.checkpoints_dir, exist_ok=True)


os.makedirs("sample", exist_ok=True)
opt = TrainOptions().parse()
iter_path = os.path.join(opt.checkpoints_dir, opt.name, "iter.txt")

torch.cuda.set_device(opt.gpu_ids[0])
torch.distributed.init_process_group("nccl", init_method="env://")
device = torch.device(f"cuda:{opt.gpu_ids[0]}")

start_epoch, epoch_iter = 1, 0

# train_data = DressCodeDataset(dataroot_path=opt.dataroot, phase='train', category=['upper_body'])
train_data = LoadVITONDataset(path=opt.dataroot, phase="train", size=(256, 192))
train_loader = DataLoader(
    train_data, batch_size=opt.batchSize, shuffle=True, num_workers=16
)

dataset_size = len(train_loader)

warp_model = PBAFWM(opt, 45)
warp_model.train()
warp_model.to(device)
load_checkpoint_parallel(warp_model, opt.PBAFN_warp_checkpoint, device)

gen_model = ResUnetGenerator(8, 4, 5, ngf=64, norm_layer=nn.BatchNorm2d)
gen_model.train()
gen_model.to(device)
# load_checkpoint_parallel(gen_model, opt.PBAFN_gen_checkpoint, device)

if opt.isTrain and len(opt.gpu_ids):
    model = torch.nn.parallel.DistributedDataParallel(
        warp_model, device_ids=[opt.gpu_ids[0]]
    )
    model_gen = torch.nn.parallel.DistributedDataParallel(
        gen_model, device_ids=[opt.gpu_ids[0]]
    )

criterionL1 = nn.L1Loss()
criterionVGG = VGGLoss()
# optimizer
params_warp = [p for p in model.parameters()]
params_gen = [p for p in model_gen.parameters()]
optimizer_warp = torch.optim.Adam(
    params_warp, lr=0.2 * opt.lr, betas=(opt.beta1, 0.999)
)
optimizer_gen = torch.optim.Adam(params_gen, lr=opt.lr, betas=(opt.beta1, 0.999))

total_steps = (start_epoch - 1) * dataset_size + epoch_iter

step = 0
step_per_batch = dataset_size

if opt.local_rank == 0:
    writer = SummaryWriter(path)

all_steps = dataset_size * (opt.niter + opt.niter_decay + 1 - start_epoch)

# for epoch in range(start_epoch, opt.niter + opt.niter_decay + 1):
for epoch in range(3):
    epoch_start_time = time.time()
    if epoch != start_epoch:
        epoch_iter = epoch_iter % dataset_size

    train_warp_loss = 0
    train_gen_loss = 0

    for i, data in enumerate(train_loader):
        iter_start_time = time.time()

        total_steps += 1
        epoch_iter += 1
        save_fake = True

        warp_loss, gen_loss, loss_all = run_once(
            opt,
            data,
            step,
            device,
            model,
            model_gen,
            TVLoss,
            criterionL1,
            criterionVGG,
            writer,
        )

        train_warp_loss += warp_loss
        train_gen_loss += gen_loss

        optimizer_warp.zero_grad()
        optimizer_gen.zero_grad()
        loss_all.backward()
        optimizer_warp.step()
        optimizer_gen.step()

        step += 1
        iter_end_time = time.time()
        iter_delta_time = iter_end_time - iter_start_time
        step_delta = (step_per_batch - step % step_per_batch) + step_per_batch * (
            opt.niter + opt.niter_decay - epoch
        )
        eta = iter_delta_time * step_delta
        eta = str(datetime.timedelta(seconds=int(eta)))
        time_stamp = datetime.datetime.now()
        now = time_stamp.strftime("%Y.%m.%d-%H:%M:%S")

        if step % 100 == 0:
            if opt.local_rank == 0:
                print(
                    "{}:{}:[step-{}/{}: {:.2%}]--[loss-{:.6f}: wl-{:.6f}, gl-{:.6f}]--[lr-{:.6f}]--[ETA-{}]".format(
                        now,
                        epoch_iter,
                        step,
                        all_steps,
                        step / all_steps,
                        loss_all,
                        warp_loss,
                        gen_loss,
                        model.module.old_lr,
                        eta,
                    )
                )

        if epoch_iter >= dataset_size:
            break

    # Visualize train loss
    train_warp_loss /= len(train_loader)
    train_gen_loss /= len(train_loader)
    train_loss = train_warp_loss * 0.5 + train_gen_loss * 1.0
    writer.add_scalar("train_warp_loss", train_warp_loss, epoch)
    writer.add_scalar("train_gen_loss", train_gen_loss, epoch)
    writer.add_scalar("train_loss", train_loss, epoch)

    iter_end_time = time.time()
    if opt.local_rank == 0:
        print(
            "End of epoch %d / %d: train_loss: %.3f \t time: %d sec"
            % (
                epoch,
                opt.niter + opt.niter_decay,
                train_loss,
                time.time() - epoch_start_time,
            )
        )

    ### save model for this epoch
    if epoch % opt.save_epoch_freq == 0:
        if opt.local_rank == 0:
            print(
                "Saving the model at the end of epoch %d, iters %d"
                % (epoch, total_steps)
            )
            save_checkpoint(
                model.module,
                os.path.join(
                    opt.checkpoints_dir,
                    opt.name,
                    "PBAFN_warp_epoch_%03d.pth" % (epoch + 1),
                ),
            )
            save_checkpoint(
                model_gen.module,
                os.path.join(
                    opt.checkpoints_dir,
                    opt.name,
                    "PBAFN_gen_epoch_%03d.pth" % (epoch + 1),
                ),
            )

    if epoch > opt.niter:
        model.module.update_learning_rate_warp(optimizer_warp)
        model.module.update_learning_rate(optimizer_gen)
