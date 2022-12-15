import torch
import torch.nn as nn
# from networks.segformer import *
# For jupyter notebook below
from segformer import *
from typing import Tuple
from einops import rearrange
from einops.layers.torch import Rearrange
from torch.nn import functional as F


class EfficientAttention(nn.Module):
    """
        input  -> x:[B, D, H, W]
        output ->   [B, D, H, W]
    
        in_channels:    int -> Embedding Dimension 
        key_channels:   int -> Key Embedding Dimension,   Best: (in_channels)
        value_channels: int -> Value Embedding Dimension, Best: (in_channels or in_channels//2) 
        head_count:     int -> It divides the embedding dimension by the head_count and process each part individually
        
        Conv2D # of Params:  ((k_h * k_w * C_in) + 1) * C_out)
    """
    
    def __init__(self, in_channels, key_channels, value_channels, head_count=1):
        super().__init__()
        self.in_channels = in_channels
        self.key_channels = key_channels
        self.head_count = head_count
        self.value_channels = value_channels

        self.keys = nn.Conv2d(in_channels, key_channels, 1) 
        self.queries = nn.Conv2d(in_channels, key_channels, 1)
        self.values = nn.Conv2d(in_channels, value_channels, 1)
        self.reprojection = nn.Conv2d(value_channels, in_channels, 1)

        
    def forward(self, input_):
        n, _, h, w = input_.size()
        # n, _,  = input_.size()
        keys = self.keys(input_).reshape((n, self.key_channels, h * w))
        queries = self.queries(input_).reshape(n, self.key_channels, h * w)
        values = self.values(input_).reshape((n, self.value_channels, h * w))
        
        head_key_channels = self.key_channels // self.head_count
        head_value_channels = self.value_channels // self.head_count
        
        attended_values = []
        for i in range(self.head_count):
            key = F.softmax(keys[
                :,
                i * head_key_channels: (i + 1) * head_key_channels,
                :
            ], dim=2)
            
            query = F.softmax(queries[
                :,
                i * head_key_channels: (i + 1) * head_key_channels,
                :
            ], dim=1)
                        
            value = values[
                :,
                i * head_value_channels: (i + 1) * head_value_channels,
                :
            ]            
            
            context = key @ value.transpose(1, 2) # dk*dv
            attended_value = (context.transpose(1, 2) @ query).reshape(n, head_value_channels, h, w) # n*dv            
            attended_values.append(attended_value)
                
        aggregated_values = torch.cat(attended_values, dim=1)
        attention = self.reprojection(aggregated_values)

        return attention
    
    
class EfficientTransformerBlock(nn.Module):
    """
        Input  -> x (Size: (b, (H*W), d)), H, W
        Output -> (b, (H*W), d)
    """
    def __init__(self, in_dim, key_dim, value_dim, head_count=1, token_mlp='mix'):
        super().__init__()
        self.norm1 = nn.LayerNorm(in_dim)
        self.attn = EfficientAttention(in_channels=in_dim, key_channels=key_dim,
                                       value_channels=value_dim, head_count=1)
        self.norm2 = nn.LayerNorm(in_dim)
        if token_mlp=='mix':
            self.mlp = MixFFN(in_dim, int(in_dim*4))  
        elif token_mlp=='mix_skip':
            self.mlp = MixFFN_skip(in_dim, int(in_dim*4)) 
        else:
            self.mlp = MLP_FFN(in_dim, int(in_dim*4))

    def forward(self, x: torch.Tensor, H, W) -> torch.Tensor:
        norm_1 = self.norm1(x)
        norm_1 = Rearrange('b (h w) d -> b d h w', h=H, w=W)(norm_1)
        
        attn = self.attn(norm_1)
        attn = Rearrange('b d h w -> b (h w) d')(attn)
        
        tx = x + attn
        mx = tx + self.mlp(self.norm2(tx), H, W)
        return mx
    
# Encoder
class MiT(nn.Module):
    def __init__(self, image_size, in_dim, key_dim, value_dim, layers, head_count=1, token_mlp='mix_skip'):
        super().__init__()
        patch_sizes = [7, 3, 3, 3]
        strides = [4, 2, 2, 2]
        padding_sizes = [3, 1, 1, 1]

        
        # patch_embed
        # layers = [2, 2, 2, 2] dims = [64, 128, 320, 512]
        self.patch_embed1 = OverlapPatchEmbeddings(image_size, patch_sizes[0], strides[0], padding_sizes[0], 3, in_dim[0])
        self.patch_embed2 = OverlapPatchEmbeddings(image_size//4, patch_sizes[1], strides[1], padding_sizes[1],in_dim[0], in_dim[1])
        self.patch_embed3 = OverlapPatchEmbeddings(image_size//8, patch_sizes[2], strides[2], padding_sizes[2],in_dim[1], in_dim[2])
        self.patch_embed4 = OverlapPatchEmbeddings(image_size//16, patch_sizes[3], strides[3], padding_sizes[3],in_dim[2], in_dim[3])
        
        # transformer encoder
        self.block1 = nn.ModuleList([ 
            EfficientTransformerBlock(in_dim[0], key_dim[0], value_dim[0], head_count, token_mlp)
        for _ in range(layers[0])])
        self.norm1 = nn.LayerNorm(in_dim[0])

        self.block2 = nn.ModuleList([
            EfficientTransformerBlock(in_dim[1], key_dim[1], value_dim[1], head_count, token_mlp)
        for _ in range(layers[1])])
        self.norm2 = nn.LayerNorm(in_dim[1])

        self.block3 = nn.ModuleList([
            EfficientTransformerBlock(in_dim[2], key_dim[2], value_dim[2], head_count, token_mlp)
        for _ in range(layers[2])])
        self.norm3 = nn.LayerNorm(in_dim[2])

        self.block4 = nn.ModuleList([
            EfficientTransformerBlock(in_dim[3], key_dim[3], value_dim[3], head_count, token_mlp)
        for _ in range(layers[3])])
        self.norm4 = nn.LayerNorm(in_dim[3])
        

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B = x.shape[0]
        outs = []

        # stage 1
        x, H, W = self.patch_embed1(x)
        for blk in self.block1:
            x = blk(x, H, W)
        x = self.norm1(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        # stage 2
        x, H, W = self.patch_embed2(x)
        for blk in self.block2:
            x = blk(x, H, W)
        x = self.norm2(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        # stage 3
        x, H, W = self.patch_embed3(x)
        for blk in self.block3:
            x = blk(x, H, W)
        x = self.norm3(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        # stage 4
        x, H, W = self.patch_embed4(x)
        for blk in self.block4:
            x = blk(x, H, W)
        x = self.norm4(x)
        x = x.reshape(B, H, W, -1).permute(0, 3, 1, 2).contiguous()
        outs.append(x)

        return outs
    
# Decoder    
class PatchExpand(nn.Module):
    def __init__(self, input_resolution, dim, dim_scale=2, norm_layer=nn.LayerNorm):
        super().__init__()
        self.input_resolution = input_resolution
        self.dim = dim
        self.expand = nn.Linear(dim, 2*dim, bias=False) if dim_scale==2 else nn.Identity()
        self.norm = norm_layer(dim // dim_scale)

    def forward(self, x):
        """
        x: B, H*W, C
        """
        # print("x_shape-----",x.shape)
        H, W = self.input_resolution
        x = self.expand(x)
        
        B, L, C = x.shape
        # print(x.shape)
        assert L == H * W, "input feature has wrong size"

        x = x.view(B, H, W, C)
        x = rearrange(x, 'b h w (p1 p2 c)-> b (h p1) (w p2) c', p1=2, p2=2, c=C//4)
        x = x.view(B,-1,C//4)
        x= self.norm(x.clone())

        return x
    
    
class FinalPatchExpand_X4(nn.Module):
    def __init__(self, input_resolution, dim, dim_scale=4, norm_layer=nn.LayerNorm):
        super().__init__()
        self.input_resolution = input_resolution
        self.dim = dim
        self.dim_scale = dim_scale
        self.expand = nn.Linear(dim, 16*dim, bias=False)
        self.output_dim = dim 
        self.norm = norm_layer(self.output_dim)

    def forward(self, x):
        """
        x: B, H*W, C
        """
        H, W = self.input_resolution
        x = self.expand(x)
        B, L, C = x.shape
        assert L == H * W, "input feature has wrong size"

        x = x.view(B, H, W, C)
        x = rearrange(x, 'b h w (p1 p2 c)-> b (h p1) (w p2) c', p1=self.dim_scale, p2=self.dim_scale, c=C//(self.dim_scale**2))
        x = x.view(B,-1,self.output_dim)
        x= self.norm(x.clone())

        return x
    
    
class MyDecoderLayer(nn.Module):
    def __init__(self, input_size, in_out_chan, head_count, token_mlp_mode, n_class=9,
                 norm_layer=nn.LayerNorm, is_last=False):
        super().__init__()
        dims = in_out_chan[0]
        out_dim = in_out_chan[1]
        key_dim = in_out_chan[2]
        value_dim = in_out_chan[3]
        if not is_last:
            self.concat_linear = nn.Linear(dims*2, out_dim)
            # transformer decoder
            self.layer_up = PatchExpand(input_resolution=input_size, dim=out_dim, dim_scale=2, norm_layer=norm_layer)
            self.last_layer = None
        else:
            self.concat_linear = nn.Linear(dims*4, out_dim)
            # transformer decoder
            self.layer_up = FinalPatchExpand_X4(input_resolution=input_size, dim=out_dim, dim_scale=4, norm_layer=norm_layer)
            # self.last_layer = nn.Linear(out_dim, n_class)
            self.last_layer = nn.Conv2d(out_dim, n_class,1)
            # self.last_layer = None

        self.layer_former_1 = EfficientTransformerBlock(out_dim, key_dim, value_dim, head_count, token_mlp_mode)
        self.layer_former_2 = EfficientTransformerBlock(out_dim, key_dim, value_dim, head_count, token_mlp_mode)
       

        def init_weights(self): 
            for m in self.modules():
                if isinstance(m, nn.Linear):
                    nn.init.xavier_uniform_(m.weight)
                    if m.bias is not None:
                        nn.init.zeros_(m.bias)
                elif isinstance(m, nn.LayerNorm):
                    nn.init.ones_(m.weight)
                    nn.init.zeros_(m.bias)
                elif isinstance(m, nn.Conv2d):
                    nn.init.xavier_uniform_(m.weight)
                    if m.bias is not None:
                        nn.init.zeros_(m.bias)

        init_weights(self)
      
    def forward(self, x1, x2=None):
        if x2 is not None:# skip connection exist
            b, h, w, c = x2.shape
            x2 = x2.view(b, -1, c)
            cat_x = torch.cat([x1, x2], dim=-1)
            cat_linear_x = self.concat_linear(cat_x)
            tran_layer_1 = self.layer_former_1(cat_linear_x, h, w)
            tran_layer_2 = self.layer_former_2(tran_layer_1, h, w)
            
            if self.last_layer:
                out = self.last_layer(self.layer_up(tran_layer_2).view(b, 4*h, 4*w, -1).permute(0,3,1,2)) 
            else:
                out = self.layer_up(tran_layer_2)
        else:
            # if len(x1.shape)>3:
            #     x1 = x1.permute(0,2,3,1)
            #     b, h, w, c = x1.shape
            #     x1 = x1.view(b, -1, c)
            out = self.layer_up(x1)
        return out
    
    
class EffMISSFormer(nn.Module):
    def __init__(self, num_classes=9, head_count=1, token_mlp_mode="mix_skip"):
        super().__init__()
    
        # Encoder
        dims, key_dim, value_dim, layers = [[64, 128, 320, 512], [64, 128, 320, 512], [64, 128, 320, 512], [2, 2, 2, 2]]        
        self.backbone = MiT(image_size=224, in_dim=dims, key_dim=key_dim, value_dim=value_dim, layers=layers,
                            head_count=head_count, token_mlp=token_mlp_mode)
        
        # Decoder
        d_base_feat_size = 7 #16 for 512 input size, and 7 for 224
        in_out_chan = [[32, 64, 64, 64],[144, 128, 128, 128],[288, 320, 320, 320],[512, 512, 512, 512]]  # [dim, out_dim, key_dim, value_dim]

        self.decoder_3 = MyDecoderLayer((d_base_feat_size, d_base_feat_size), in_out_chan[3], head_count, 
                                        token_mlp_mode, n_class=num_classes)
        self.decoder_2 = MyDecoderLayer((d_base_feat_size*2, d_base_feat_size*2), in_out_chan[2], head_count,
                                        token_mlp_mode, n_class=num_classes)
        self.decoder_1 = MyDecoderLayer((d_base_feat_size*4, d_base_feat_size*4), in_out_chan[1], head_count, 
                                        token_mlp_mode, n_class=num_classes) 
        self.decoder_0 = MyDecoderLayer((d_base_feat_size*8, d_base_feat_size*8), in_out_chan[0], head_count,
                                        token_mlp_mode, n_class=num_classes, is_last=True)

        
    def forward(self, x):
        #---------------Encoder-------------------------
        if x.size()[1] == 1:
            x = x.repeat(1,3,1,1)

        output_enc = self.backbone(x)

        b,c,_,_ = output_enc[3].shape

        #---------------Decoder-------------------------     
        tmp_3 = self.decoder_3(output_enc[3].permute(0,2,3,1).view(b,-1,c))
        tmp_2 = self.decoder_2(tmp_3, output_enc[2].permute(0,2,3,1))
        tmp_1 = self.decoder_1(tmp_2, output_enc[1].permute(0,2,3,1))
        tmp_0 = self.decoder_0(tmp_1, output_enc[0].permute(0,2,3,1))

        return tmp_0
    
# if __name__ == "__main__":
#     model = EffMISSFormer(num_classes=9, head_count=1, token_mlp_mode="mix_skip")
#     print(model(torch.rand(1, 3, 224, 224)).shape)
    