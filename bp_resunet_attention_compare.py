import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, ConcatDataset
import h5py
import numpy as np
from pathlib import Path
from tqdm import tqdm
import os

############################################################
# 0) Dataset
############################################################
class BPDataset(Dataset):
    """
    從 .h5 中讀取:
      - ppg: (N,1250)
      - ecg: (N,1250) 若不存在，則以 zeros 取代
      - segsbp, segdbp: (N,)
      - personal_info: (N, M) 若不存在，則以 zeros 填充 (預設 M=5)
      - vascular_properties: (N,3) 若不存在，則以 zeros 填充 (假設 3 維)
    """
    def __init__(self, h5_path):
        super().__init__()
        self.h5_path = Path(h5_path)
        with h5py.File(self.h5_path, 'r') as f:
            self.ppg = torch.from_numpy(f['ppg'][:])  # (N,1250)
            if 'ecg' in f:
                self.ecg = torch.from_numpy(f['ecg'][:])
            else:
                self.ecg = torch.zeros_like(self.ppg)
            self.sbp = torch.from_numpy(f['segsbp'][:])
            self.dbp = torch.from_numpy(f['segdbp'][:])
            if 'personal_info' in f:
                self.personal_info = torch.from_numpy(f['personal_info'][:])
            else:
                n = self.ppg.shape[0]
                self.personal_info = torch.zeros((n,5))
            if 'vascular_properties' in f:
                self.vascular = torch.from_numpy(f['vascular_properties'][:])
            else:
                n = self.ppg.shape[0]
                self.vascular = torch.zeros((n,3))
        # reshape => (N,1,1250)
        self.ppg = self.ppg.unsqueeze(1)
        self.ecg = self.ecg.unsqueeze(1)
        # 組成 bp tensor shape=(N,2)
        self.bp_2d = torch.stack([self.sbp, self.dbp], dim=1)

    def __len__(self):
        return len(self.ppg)

    def __getitem__(self, idx):
        return {
            'ppg': self.ppg[idx],                   # shape=(1,1250)
            'ecg': self.ecg[idx],                   # shape=(1,1250)
            'bp_values': self.bp_2d[idx],           # shape=(2,)
            'personal_info': self.personal_info[idx],  # shape=(M,)
            'vascular': self.vascular[idx]          # shape=(3,)
        }

############################################################
# 1) 一個簡易 ResUNet1D
############################################################
class ConvBlock1D(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_size=3):
        super().__init__()
        pad = kernel_size // 2
        self.conv = nn.Conv1d(in_ch, out_ch, kernel_size, padding=pad, bias=False)
        self.bn   = nn.BatchNorm1d(out_ch)
        self.act  = nn.ReLU(inplace=True)
    def forward(self, x):
        return self.act(self.bn(self.conv(x)))

class DownBlock(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.pool = nn.MaxPool1d(2)
        self.conv1 = ConvBlock1D(in_ch, out_ch, 3)
        self.conv2 = ConvBlock1D(out_ch, out_ch, 3)
    def forward(self, x):
        x = self.pool(x)
        x = self.conv1(x)
        x = self.conv2(x)
        return x

class UpBlock(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.upconv = nn.ConvTranspose1d(in_ch, out_ch, kernel_size=2, stride=2, bias=False)
        self.conv1 = ConvBlock1D(out_ch*2, out_ch, 3)
        self.conv2 = ConvBlock1D(out_ch, out_ch, 3)
    def forward(self, x, skip):
        x = self.upconv(x)
        # 對齊長度
        diff = skip.shape[-1] - x.shape[-1]
        if diff > 0:
            skip = skip[..., :x.shape[-1]]
        elif diff < 0:
            x = x[..., :skip.shape[-1]]
        x = torch.cat([skip, x], dim=1)
        x = self.conv1(x)
        x = self.conv2(x)
        return x

class ResUNet1D(nn.Module):
    def __init__(self, in_ch=1, out_ch=64, base_ch=16):
        """
        in_ch: 輸入通道 (預設=1)
        out_ch: 最後輸出通道 (例如用於 feature map)
        base_ch: 基礎通道數 (可根據需求調整)
        """
        super().__init__()
        # Encoder
        self.enc_conv1 = nn.Sequential(
            ConvBlock1D(in_ch, base_ch, 3),
            ConvBlock1D(base_ch, base_ch, 3)
        )
        self.down1 = DownBlock(base_ch, base_ch*2)
        self.down2 = DownBlock(base_ch*2, base_ch*4)
        self.down3 = DownBlock(base_ch*4, base_ch*8)
        # Bottleneck
        self.bottleneck = nn.Sequential(
            ConvBlock1D(base_ch*8, base_ch*8, 3),
            ConvBlock1D(base_ch*8, base_ch*8, 3)
        )
        # Decoder
        self.up1 = UpBlock(base_ch*8, base_ch*4)
        self.up2 = UpBlock(base_ch*4, base_ch*2)
        self.up3 = UpBlock(base_ch*2, base_ch)
        # Final conv
        self.final = nn.Conv1d(base_ch, out_ch, kernel_size=1, bias=False)
    def forward(self, x):
        c1 = self.enc_conv1(x)    # (B, base_ch, L)
        c2 = self.down1(c1)       # (B, base_ch*2, L/2)
        c3 = self.down2(c2)       # (B, base_ch*4, L/4)
        c4 = self.down3(c3)       # (B, base_ch*8, L/8)
        b = self.bottleneck(c4)   # (B, base_ch*8, L/8)
        d1 = self.up1(b, c3)      # (B, base_ch*4, L/4)
        d2 = self.up2(d1, c2)     # (B, base_ch*2, L/2)
        d3 = self.up3(d2, c1)     # (B, base_ch, L)
        out = self.final(d3)      # (B, out_ch, L)
        return out


############################################################
# 2) Self-Attention 1D
############################################################
class MultiHeadSelfAttn1D(nn.Module):
    """
    對 (B,C,L) 做 self-attn, B= batch, C=channel, L= seq_len
    需 C==d_model
    """
    def __init__(self, d_model=64, n_heads=4):
        super().__init__()
        self.n_heads = n_heads
        self.d_model = d_model
        self.mha = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
        self.ln = nn.LayerNorm(d_model)

    def forward(self, x):
        """
        x: (B,C,L) => 先轉 (B,L,C)
        => q,k,v = x
        => out=(B,L,C), 再->(B,C,L)
        """
        B,C,L = x.shape
        if C!=self.d_model:
            raise ValueError(f"MultiHeadSelfAttn1D expects C={self.d_model}, got {C}")
        x_t = x.transpose(1,2)  # (B,L,C)
        out, _ = self.mha(x_t, x_t, x_t)  # self-attn
        out = self.ln(out)
        # shape => (B,L,C) => (B,C,L)
        out = out.transpose(1,2)
        return out

############################################################
# 3) PPGOnly model vs. PPG+ECG model
############################################################
class ModelPPGOnly(nn.Module):
    """
    PPG + Personal Info model:
    - ResUNet1D for PPG signal
    - Self-attention on features
    - Combine with personal info
    - Final prediction
    """
    def __init__(self, info_dim=5, wave_out_ch=64, d_model=64, n_heads=4):
        super().__init__()
        self.ppg_unet = ResUNet1D(in_ch=1, out_ch=wave_out_ch)
        self.self_attn = MultiHeadSelfAttn1D(d_model=d_model, n_heads=n_heads)
        self.final_pool = nn.AdaptiveAvgPool1d(1)
        
        # personal info embedding
        self.info_fc = nn.Sequential(
            nn.Linear(info_dim, 32),
            nn.ReLU()
        )
        
        # final prediction
        self.final_fc = nn.Sequential(
            nn.Linear(wave_out_ch+32, 64),
            nn.ReLU(),
            nn.Linear(64, 2)
        )
    
    def forward(self, ppg, personal_info):
        # PPG processing
        x = self.ppg_unet(ppg)           # (B,64,L)
        x = self.self_attn(x)            # (B,64,L)
        x = self.final_pool(x)           # (B,64,1)
        wave_feat = x.squeeze(-1)        # (B,64)
        
        # Personal info processing
        info_feat = self.info_fc(personal_info)  # (B,32)
        
        # Combine features
        combined = torch.cat([wave_feat, info_feat], dim=1)  # (B,96)
        
        # Final prediction
        out = self.final_fc(combined)    # (B,2)
        return out

class ModelPPGECG(nn.Module):
    """
    模型架構：
      - 分別用 ResUNet1D 處理 ppg 與 ecg 信號
      - 各自經 self-attention 及 global average pooling 得到特徵向量 (B, wave_out_ch)
      - 個人資訊經 info_fc 處理成 (B, 32)
      - vascular_properties 經 vasc_fc 處理成 (B, 32)
      - 將上述特徵串接後 (B, 2*wave_out_ch + 64)，再經全連接層預測血壓 (2 維: [SBP, DBP])
    """
    def __init__(self, info_dim=4, vascular_dim=3, wave_out_ch=64, d_model=64, n_heads=4):
        super().__init__()
        self.ppg_unet = ResUNet1D(in_ch=1, out_ch=wave_out_ch)
        self.ecg_unet = ResUNet1D(in_ch=1, out_ch=wave_out_ch)
        self.self_attn_ppg = MultiHeadSelfAttn1D(d_model=d_model, n_heads=n_heads)
        self.self_attn_ecg = MultiHeadSelfAttn1D(d_model=d_model, n_heads=n_heads)
        self.final_pool = nn.AdaptiveAvgPool1d(1)
        # personal info 處理
        self.info_fc = nn.Sequential(
            nn.Linear(info_dim, 32),
            nn.ReLU()
        )
        # vascular_properties 處理
        self.vasc_fc = nn.Sequential(
            nn.Linear(vascular_dim, 32),
            nn.ReLU()
        )
        # 最終全連接層：輸入維度 = wave_out_ch*2 + 32 + 32
        self.final_fc = nn.Sequential(
            nn.Linear(wave_out_ch*2 + 64, 64),
            nn.ReLU(),
            nn.Linear(64, 2)
        )
    def forward(self, ppg, ecg, personal_info, vascular):
        # 處理 ppg 與 ecg 信號
        ppg_feat_map = self.ppg_unet(ppg)    # (B, wave_out_ch, L)
        ecg_feat_map = self.ecg_unet(ecg)      # (B, wave_out_ch, L)
        ppg_feat_map = self.self_attn_ppg(ppg_feat_map)  # (B, wave_out_ch, L)
        ecg_feat_map = self.self_attn_ecg(ecg_feat_map)    # (B, wave_out_ch, L)
        ppg_feat = self.final_pool(ppg_feat_map).squeeze(-1)  # (B, wave_out_ch)
        ecg_feat = self.final_pool(ecg_feat_map).squeeze(-1)  # (B, wave_out_ch)
        # 個人資訊與 vascular_properties 分別處理
        info_feat = self.info_fc(personal_info)   # (B, 32)
        vasc_feat = self.vasc_fc(vascular)         # (B, 32)
        # 串接所有特徵
        combined = torch.cat([ppg_feat, ecg_feat, info_feat, vasc_feat], dim=1)  # (B, wave_out_ch*2 + 64)
        out = self.final_fc(combined)  # (B,2)
        return out

############################################################
# 4) Trainer: 兩種模式
############################################################
class BPTrainerCompare:
    """
    讀取 dataset: (ppg, ecg, personal_info)
    1) ModelPPGOnly => ppg + personal_info
    2) ModelPPGECG => ppg + ecg + personal_info
    分別訓練 & 評估 MSE/MAE
    """
    def __init__(self, fold_path, device='cuda', batch_size=32, lr=1e-3):
        self.fold_path = Path(fold_path)
        self.device = torch.device(device)
        self.batch_size = batch_size
        self.lr = lr

    def create_dataloaders(self):
        # 假設跟您原本做法: training_1~9, validation, test
        train_files = [self.fold_path/f"training_{i}.h5" for i in range(1,10)]
        from torch.utils.data import ConcatDataset
        train_ds_list = []
        for tf in train_files:
            if tf.exists():
                train_ds_list.append( BPDataset(tf) )
        train_dataset = ConcatDataset(train_ds_list)

        val_dataset  = BPDataset(self.fold_path/'validation.h5')
        test_dataset = BPDataset(self.fold_path/'test.h5')

        train_loader = DataLoader(train_dataset, batch_size=self.batch_size, shuffle=True, drop_last=True)
        val_loader   = DataLoader(val_dataset,  batch_size=self.batch_size, shuffle=False, drop_last=False)
        test_loader  = DataLoader(test_dataset, batch_size=self.batch_size, shuffle=False, drop_last=False)

        return train_loader, val_loader, test_loader

    def train_one_model(self, model, train_loader, val_loader, max_epochs=50, early_stop_patience=10):
        model.to(self.device)
        criterion = nn.MSELoss()
        optimizer = optim.Adam(model.parameters(), lr=self.lr, weight_decay=1e-4)
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=3, min_lr=1e-6)

        best_val_loss = float('inf')
        patience_count = 0

        for epoch in tqdm(range(1, max_epochs+1)):
            # train
            model.train()
            total_loss=0.0
            total_mae=0.0
            for batch in tqdm(train_loader):
                ppg  = batch['ppg'].to(self.device)
                ecg  = batch['ecg'].to(self.device)
                bp   = batch['bp_values'].float().to(self.device)
                info = batch['personal_info'].float().to(self.device)
                # print(f'ppg shape: {ppg.shape}, ecg shape: {ecg.shape}, bp shape: {bp.shape}, info shape: {info.shape}')
                optimizer.zero_grad()
                # 依模型不同 => forward
                if isinstance(model, ModelPPGOnly):
                    preds = model(ppg, info)
                else:
                    preds = model(ppg, ecg, info)

                loss = criterion(preds, bp)
                mae  = (preds - bp).abs().mean()
                loss.backward()
                optimizer.step()
                total_loss += loss.item()
                total_mae  += mae.item()

            train_loss = total_loss/len(train_loader)
            train_mae  = total_mae/len(train_loader)

            # val
            model.eval()
            val_loss_agg=0.0
            val_mae_agg =0.0
            with torch.no_grad():
                for batch in tqdm(val_loader):
                    ppg  = batch['ppg'].to(self.device)
                    ecg  = batch['ecg'].to(self.device)
                    bp   = batch['bp_values'].float().to(self.device)
                    info = batch['personal_info'].float().to(self.device)
                    if isinstance(model, ModelPPGOnly):
                        preds = model(ppg, info)
                    else:
                        preds = model(ppg, ecg, info)
                    l = criterion(preds, bp)
                    m = (preds - bp).abs().mean()
                    val_loss_agg += l.item()
                    val_mae_agg  += m.item()

            val_loss = val_loss_agg/len(val_loader)
            val_mae  = val_mae_agg /len(val_loader)
            scheduler.step(val_loss)

            if val_loss<best_val_loss:
                best_val_loss=val_loss
                patience_count=0
                best_weights= model.state_dict()
            else:
                patience_count+=1
                if patience_count>=early_stop_patience:
                    print(f"[EarlyStop] epoch={epoch}")
                    break

            print(f"[Epoch {epoch}/{max_epochs}] TrainLoss={train_loss:.4f},MAE={train_mae:.4f} | ValLoss={val_loss:.4f},MAE={val_mae:.4f}")

        if best_weights:
            model.load_state_dict(best_weights)
        return model

    def eval_model(self, model, test_loader):
        model.eval()
        criterion = nn.MSELoss()
        total_loss=0.0
        total_mae =0.0
        total_count=0
        with torch.no_grad():
            for batch in test_loader:
                ppg  = batch['ppg'].to(self.device)
                ecg  = batch['ecg'].to(self.device)
                bp   = batch['bp_values'].float().to(self.device)
                info = batch['personal_info'].float().to(self.device)

                if isinstance(model, ModelPPGOnly):
                    preds = model(ppg, info)
                else:
                    preds = model(ppg, ecg, info)
                loss = criterion(preds, bp)
                mae  = (preds - bp).abs().mean()
                total_loss+= loss.item()
                total_mae += mae.item()
                total_count+=1

        test_loss= total_loss/ total_count
        test_mae = total_mae / total_count
        return test_loss, test_mae

    def run_compare(self):
        """
        1) create loader
        2) train ModelPPGOnly => get test MSE/MAE
        3) train ModelPPGECG  => get test MSE/MAE
        4) compare
        """
        train_loader, val_loader, test_loader = self.create_dataloaders()
        print(f'train_loader: {len(train_loader)}, val_loader: {len(val_loader)}, test_loader: {len(test_loader)}')
        # 1) PPGOnly
        print("\n=== Training ModelPPGOnly ===")
        model_ppg_only = ModelPPGOnly(info_dim=5, wave_out_ch=64, d_model=64, n_heads=4)
        #計算參數
        total_params = sum(p.numel() for p in model_ppg_only.parameters())
        print(f"Total parameters: {total_params}")
        model_ppg_only = self.train_one_model(model_ppg_only, train_loader, val_loader)
        ppg_only_test_loss, ppg_only_test_mae = self.eval_model(model_ppg_only, test_loader)
        print(f"[PPGOnly] TestLoss={ppg_only_test_loss:.4f}, TestMAE={ppg_only_test_mae:.4f}")

        # 2) PPG+ECG
        print("\n=== Training ModelPPGECG ===")
        model_ppg_ecg = ModelPPGECG(info_dim=4, wave_out_ch=64, d_model=64, n_heads=4)
        total_params = sum(p.numel() for p in model_ppg_ecg.parameters())
        print(f"Total parameters: {total_params}")
        model_ppg_ecg = self.train_one_model(model_ppg_ecg, train_loader, val_loader)
        ppg_ecg_test_loss, ppg_ecg_test_mae = self.eval_model(model_ppg_ecg, test_loader)
        print(f"[PPG+ECG] TestLoss={ppg_ecg_test_loss:.4f}, TestMAE={ppg_ecg_test_mae:.4f}")

        print("\n=== Compare ===")
        print(f"PPGOnly => MSE={ppg_only_test_loss:.4f}, MAE={ppg_only_test_mae:.4f}")
        print(f"PPG+ECG => MSE={ppg_ecg_test_loss:.4f}, MAE={ppg_ecg_test_mae:.4f}")

############################################################
# main
############################################################
# if __name__=='__main__':
#     fold_path='training_data_1250_MIMIC'  # e.g. 您資料夾
#     trainer = BPTrainerCompare(
#         fold_path=fold_path,
#         device='cuda',
#         batch_size=32,
#         lr=1e-3
#     )
#     trainer.run_compare()


########################################
#  1) Dataset (同樣保留 BPDataset)
########################################

########################################
#  2) ResUNet1D (可再加深加寬)
########################################
class ConvBlock1D(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_size=3):
        super().__init__()
        pad = kernel_size // 2
        self.conv = nn.Conv1d(in_ch, out_ch, kernel_size, padding=pad, bias=False)
        self.bn   = nn.BatchNorm1d(out_ch)
        self.act  = nn.ReLU(inplace=True)
    def forward(self, x):
        return self.act(self.bn(self.conv(x)))

class DownBlock(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.pool = nn.MaxPool1d(2)
        self.conv1= ConvBlock1D(in_ch, out_ch, 3)
        self.conv2= ConvBlock1D(out_ch,out_ch, 3)
    def forward(self, x):
        x = self.pool(x)
        x = self.conv1(x)
        x = self.conv2(x)
        return x

class UpBlock(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.upconv= nn.ConvTranspose1d(in_ch, out_ch, kernel_size=2, stride=2, bias=False)
        self.conv1 = ConvBlock1D(out_ch*2, out_ch, 3)
        self.conv2 = ConvBlock1D(out_ch,   out_ch, 3)

    def forward(self, x, skip):
        x = self.upconv(x)
        # 簡單對齊
        diff = skip.shape[-1] - x.shape[-1]
        if diff>0:
            skip = skip[...,:-diff]
        elif diff<0:
            x = x[...,:skip.shape[-1]]
        x = torch.cat([skip, x], dim=1)
        x = self.conv1(x)
        x = self.conv2(x)
        return x

# class ResUNet1D(nn.Module):
#     def __init__(self, in_ch=1, base_ch=16): 
#         """
#         base_ch=32 => 依需求加大
#         """
#         super().__init__()
#         # encoder
#         self.enc_conv1= nn.Sequential(
#             ConvBlock1D(in_ch, base_ch, 3),
#             ConvBlock1D(base_ch, base_ch, 3)
#         )
#         self.down1= DownBlock(base_ch, base_ch*2)
#         self.down2= DownBlock(base_ch*2,base_ch*4)

#         # bottleneck
#         self.bottleneck= nn.Sequential(
#             ConvBlock1D(base_ch*4, base_ch*4,3),
#             ConvBlock1D(base_ch*4, base_ch*4,3),
#         )

#         # decoder
#         self.up1= UpBlock(base_ch*4, base_ch*2)
#         self.up2= UpBlock(base_ch*2, base_ch)

#         self.final= nn.Conv1d(base_ch, base_ch, kernel_size=1,bias=False)
#         # 最終輸出channel=base_ch

#     def forward(self, x):
#         c1 = self.enc_conv1(x)   # (B,16, L)
#         c2 = self.down1(c1)      # (B,32, L/2)
#         c3 = self.down2(c2)      # (B,64, L/4)

#         b  = self.bottleneck(c3) # (B,64, L/4)

#         d1 = self.up1(b,  c2)    # =>(B,32, L/2)
#         d2 = self.up2(d1, c1)    # =>(B,16, L)
#         out= self.final(d2)      # =>(B,16,L)
#         return out

########################################
# 3) MultiHeadSelfAttn1D
########################################
class MultiHeadSelfAttn1D(nn.Module):
    def __init__(self, d_model=64, n_heads=4):
        super().__init__()
        self.n_heads = n_heads
        self.d_model = d_model
        self.mha = nn.MultiheadAttention(d_model, n_heads, batch_first=True)
        self.ln = nn.LayerNorm(d_model)
    def forward(self, x):
        # x: (B, C, L) ，要求 C == d_model
        B, C, L = x.shape
        if C != self.d_model:
            raise ValueError(f"MultiHeadSelfAttn1D expects channel {self.d_model}, got {C}")
        x_t = x.transpose(1,2)  # (B, L, C)
        out, _ = self.mha(x_t, x_t, x_t)
        out = self.ln(out)      # (B, L, C)
        out = out.transpose(1,2)  # (B, C, L)
        return out
########################################
# 4) Model: PPGOnlyNoInfo
########################################
class ModelPPGOnlyNoInfo(nn.Module):
    """
    只用 PPG => ResUNet1D => SelfAttn => GlobalAvg => FC => [SBP, DBP]
    不含 personal_info
    """
    def __init__(self, base_ch=16, attn_dim=32, n_heads=4):
        super().__init__()
        # 1) ResUNet
        self.unet= ResUNet1D(in_ch=1, base_ch=base_ch)
        # unet 輸出channel= base_ch, 需= attn_dim => conv1x1
        self.align_conv= nn.Conv1d(base_ch, attn_dim, 1, bias=False)

        # 2) self-attn
        self.self_attn= MultiHeadSelfAttn1D(d_model=attn_dim, n_heads=n_heads)
        self.global_pool= nn.AdaptiveAvgPool1d(1)

        # 3) final fc
        self.final_fc= nn.Sequential(
            nn.Linear(attn_dim, 32),
            nn.ReLU(),
            nn.Linear(32,16),
            nn.ReLU(),
            nn.Linear(16,2)
        )

    def forward(self, ppg):
        """
        ppg: (B,1,1250)
        return: (B,2)
        """
        x= self.unet(ppg)          # => (B, base_ch, L)
        # print(f'unet shape={x.shape}')
        x= self.align_conv(x)      # => (B, attn_dim, L)
        # print(f'align_conv shape={x.shape}')
        x= self.self_attn(x)       # => (B, attn_dim, L)
        # input(f'self_attn shape={x.shape}')
        x= self.global_pool(x)     # => (B, attn_dim,1)
        feat= x.squeeze(-1)        # => (B, attn_dim)
        out= self.final_fc(feat)   # => (B,2)
        return out

#########################################################
# 5) Trainer: Compare 3 models
#########################################################
class BPTrainerCompare3:
    """
    比較三種模型：
      1) ModelPPGOnlyNoInfo (僅用 ppg)
      2) ModelPPGOnly (用 ppg 與 personal_info)
      3) ModelPPGECG (用 ppg、ecg、personal_info 以及 vascular_properties)
      
    本範例著重修改 ModelPPGECG，使其納入 vascular_properties
    """
    def __init__(self, fold_path, device='cuda', batch_size=32, lr=1e-3):
        self.fold_path = Path(fold_path)
        self.device = torch.device(device)
        self.batch_size = batch_size
        self.lr = lr

    def create_dataloaders(self):
        train_files = [self.fold_path / f"training_{i}.h5" for i in range(1,10)]
        ds_list = []
        for tf in train_files:
            if tf.exists():
                ds_list.append(BPDataset(tf))
        train_ds = ConcatDataset(ds_list)
        val_ds = BPDataset(self.fold_path / 'validation.h5')
        test_ds = BPDataset(self.fold_path / 'test.h5')
        train_loader = DataLoader(train_ds, batch_size=self.batch_size, shuffle=True, drop_last=True)
        val_loader = DataLoader(val_ds, batch_size=self.batch_size, shuffle=False, drop_last=False)
        test_loader = DataLoader(test_ds, batch_size=self.batch_size, shuffle=False, drop_last=False)
        return train_loader, val_loader, test_loader
    def train_model(self, model, train_loader, val_loader, epochs=50, early_stop_patience=10):
        print(f"Device={self.device}")
        model.to(self.device)
        print(f"Model parameters: {sum(p.numel() for p in model.parameters())}")
        criterion = nn.MSELoss()
        optimizer = optim.Adam(model.parameters(), lr=self.lr, weight_decay=1e-4)
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=3, min_lr=1e-6)
        best_val = float('inf')
        best_sd = None
        patience_count = 0
        for ep in tqdm(range(1, epochs+1)):
            model.train()
            run_loss = 0.0
            run_mae = 0.0
            for batch in tqdm(train_loader):
                ppg = batch['ppg'].to(self.device)
                ecg = batch['ecg'].to(self.device)
                bp = batch['bp_values'].float().to(self.device)
                info = batch['personal_info'].float().to(self.device)
                vascular = batch['vascular'].float().to(self.device)
                optimizer.zero_grad()
                # 依模型不同呼叫 forward
                # 假設若模型不是 ModelPPGOnlyNoInfo 或 ModelPPGOnly 則為 ModelPPGECG
                if hasattr(model, 'ppg_unet') and hasattr(model, 'ecg_unet') and hasattr(model, 'vasc_fc'):
                    preds = model(ppg, ecg, info, vascular)
                elif hasattr(model, 'ppg_unet') and hasattr(model, 'info_fc'):
                    preds = model(ppg, info)
                else:
                    preds = model(ppg)
                loss = criterion(preds, bp)
                mae = (preds - bp).abs().mean()
                loss.backward()
                optimizer.step()
                run_loss += loss.item()
                run_mae += mae.item()
            train_loss = run_loss / len(train_loader)
            train_mae = run_mae / len(train_loader)
            # validation
            model.eval()
            val_loss_sum = 0.0
            val_mae_sum = 0.0
            with torch.no_grad():
                for batch in tqdm(val_loader):
                    ppg = batch['ppg'].to(self.device)
                    ecg = batch['ecg'].to(self.device)
                    bp = batch['bp_values'].float().to(self.device)
                    info = batch['personal_info'].float().to(self.device)
                    vascular = batch['vascular'].float().to(self.device)
                    if hasattr(model, 'ppg_unet') and hasattr(model, 'ecg_unet') and hasattr(model, 'vasc_fc'):
                        preds = model(ppg, ecg, info, vascular)
                    elif hasattr(model, 'ppg_unet') and hasattr(model, 'info_fc'):
                        preds = model(ppg, info)
                    else:
                        preds = model(ppg)
                    l = criterion(preds, bp)
                    m = (preds - bp).abs().mean()
                    val_loss_sum += l.item()
                    val_mae_sum += m.item()
            val_loss = val_loss_sum / len(val_loader)
            val_mae = val_mae_sum / len(val_loader)
            scheduler.step(val_loss)
            if val_loss < best_val:
                best_val = val_loss
                patience_count = 0
                best_sd = model.state_dict()
                torch.save(model.state_dict(), f"model_{model.__class__.__name__}.pth")
            else:
                patience_count += 1
                if patience_count >= early_stop_patience:
                    print(f"[EarlyStop] epoch={ep}")
                    break
            print(f"[Epoch {ep}/{epochs}] TrainLoss={train_loss:.4f}, MAE={train_mae:.4f} | ValLoss={val_loss:.4f}, MAE={val_mae:.4f}")
        if best_sd is not None:
            model.load_state_dict(best_sd)
        return model

    def eval_model(self, model, test_loader):
        model.eval()
        criterion = nn.MSELoss()
        run_loss = 0.0
        run_mae = 0.0
        count = 0
        with torch.no_grad():
            for batch in test_loader:
                ppg = batch['ppg'].to(self.device)
                ecg = batch['ecg'].to(self.device)
                bp = batch['bp_values'].float().to(self.device)
                info = batch['personal_info'].float().to(self.device)
                vascular = batch['vascular'].float().to(self.device)
                if hasattr(model, 'ppg_unet') and hasattr(model, 'ecg_unet') and hasattr(model, 'vasc_fc'):
                    preds = model(ppg, ecg, info, vascular)
                elif hasattr(model, 'ppg_unet') and hasattr(model, 'info_fc'):
                    preds = model(ppg, info)
                else:
                    preds = model(ppg)
                l = criterion(preds, bp)
                m = (preds - bp).abs().mean()
                run_loss += l.item()
                run_mae += m.item()
                count += 1
        test_loss = run_loss / count
        test_mae = run_mae / count
        return test_loss, test_mae

    def run_all(self):
        train_loader, val_loader, test_loader = self.create_dataloaders()
        print(f"train_loader: {len(train_loader)}, val_loader: {len(val_loader)}, test_loader: {len(test_loader)}")
        # 本範例主要示範 ModelPPGECG 的訓練（納入 vascular_properties）
        print("\n=== Training ModelPPGECG (PPG + ECG + Info + Vascular) ===")
        # 參數設定：info_dim=4 (個人資訊維度)，vascular_dim=3 (vascular_properties 維度)
        model_ppg_ecg = ModelPPGECG(info_dim=4, vascular_dim=3, wave_out_ch=32, d_model=32, n_heads=4)
        model_ppg_ecg = self.train_model(model_ppg_ecg, train_loader, val_loader)
        test_loss, test_mae = self.eval_model(model_ppg_ecg, test_loader)
        print(f"[PPG+ECG+Info+Vascular] Test MSE={test_loss:.4f}, Test MAE={test_mae:.4f}")

############################################################
# main
############################################################
if __name__=='__main__':
    fold_path='training_data_VitalDB_quality'  
    trainer= BPTrainerCompare3(
        fold_path=fold_path,
        device='cuda',
        batch_size=64,
        lr=1e-3
    )
    trainer.run_all()
