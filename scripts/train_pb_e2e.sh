python train_pb_e2e.py --project runs/train/PFAFN_align_mobile_merge-viton-v2 --name FS_PB_e2e_100 \
--device 2 --align_corners --batch_size 14 --workers 16 --lr 0.00005 \
--niter 50 --niter_decay 50 --save_period 20 \
--print_step 200 --sample_step 1000 \
--dataroot ../dataset/Merge-VITON-V2/VITON_traindata \
--pb_warp_checkpoint 'runs/train/PFAFN_align_mobile_merge-viton-v2/FS_PB_warp_100/weights/pb_warp_last.pt' \