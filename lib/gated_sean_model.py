import torch
import torch.nn as nn
import torch.nn.functional as F

#from SEAN.normalization import * 
#from SEAN.spade_arch import *
from gated_modules import *

from thop import profile, clever_format

from lib.unet.unet_misc import *

class SegNet(nn.Module):
    def __init__(self, n_channels=3, n_classes=2, bilinear=True, backbone = 'full', full_size = True, decoder_type = 'none'):
        super(SegNet, self).__init__()
        self.n_channels = n_channels
        self.n_classes = n_classes
                
        self.backbone = backbone

        print('segnet backbone', self.backbone)

        self.int_channels = 64

        if(self.backbone == 'light'):
            self.int_channels = 32

        if(self.backbone == 'light_depth'):
            self.int_channels = 32
            self.n_channels = 1
                    
        self.inc = DoubleConv(self.n_channels, self.int_channels)
        self.outc = OutConv(self.int_channels, self.n_classes)
                
        self.full_size = full_size
        self.decoder_type = decoder_type

        ###FIXME - different from clutter mask
        self.default_h = 512

        self.full = ( (self.backbone == 'full') or (self.backbone == 'light') or (self.backbone == 'light_depth'))

        if(self.full):
            print('full unet')
            self.down1 = Down(self.int_channels, self.int_channels*2)
            self.down2 = Down(self.int_channels*2, self.int_channels*4)
            self.down3 = Down(self.int_channels*4, self.int_channels*8)
            factor = 2 if bilinear else 1
            self.down4 = Down(self.int_channels*8, self.int_channels*16 // factor)
            self.up1 = Up(self.int_channels*16, self.int_channels*8 // factor, bilinear)
            self.up2 = Up(self.int_channels*8, self.int_channels*4 // factor, bilinear)
            self.up3 = Up(self.int_channels*4, self.int_channels*2 // factor, bilinear)
            self.up4 = Up(self.int_channels*2, self.int_channels, bilinear)
        else:
            factor = 2 if bilinear else 1
            self.down1 = Down(self.int_channels, self.int_channels*2)
            self.down2 = Down(self.int_channels*2, self.int_channels*4 // factor)
            #self.down3 = Down(int_channels*4, int_channels*8)
            
            #self.down4 = Down(int_channels*8, int_channels*16 // factor)
            #self.up1 = Up(int_channels*16, int_channels*8 // factor, bilinear)
            #self.up2 = Up(int_channels*8, int_channels*4 // factor, bilinear)
            self.up3 = Up(self.int_channels*4, self.int_channels*2 // factor, bilinear)
            self.up4 = Up(self.int_channels*2, self.int_channels, bilinear)
        

    def forward(self, x):

        _,_,h,w = x.size()

        if(h != self.default_h and ~self.full_size):
            x = F.interpolate(x, size=(self.default_h, self.default_h), mode='bilinear', align_corners=False)
                
        x1 = self.inc(x)

        if(self.full):
            #
            x2 = self.down1(x1)
            x3 = self.down2(x2)
            x4 = self.down3(x3)
            x5 = self.down4(x4)
            ##print(x5.shape)
            x = self.up1(x5, x4)
            x = self.up2(x, x3)
            x = self.up3(x, x2)
            x = self.up4(x, x1)
            logits = self.outc(x)
        else:
            x2 = self.down1(x1)
            x3 = self.down2(x2)
            ##x4 = self.down3(x3)
            ##x5 = self.down4(x4)
            ##x = self.up1(x5, x4)
            ##x = self.up2(x, x3)              
            x = self.up3(x3, x2)
            x = self.up4(x, x1)
            logits = self.outc(x)

        if(h != self.default_h):
            logits = F.interpolate(logits, size=(h, w), mode='bilinear', align_corners=False)

        return logits

def get_segmentation_masks(seg_pred):
    soft_sem = torch.softmax(seg_pred, dim = 1) #####TO DO - here semantic is given by clutter mask
    soft_sem = torch.argmax(soft_sem, dim=1, keepdim=True)
    soft_sem = torch.clamp(soft_sem, min=0, max=1)
    masks = torch.zeros_like(seg_pred).to(seg_pred.device)
    masks.scatter_(1, soft_sem, 1)

    return masks

def get_z_encoding(img, sem_layout, device):##########NB. style codes are computed on the src pose
    
    B,C,H,W = sem_layout.size()

    num_classes = C
    in_SEAN_channels = C ####default 3
    style_code_dim = 512 ###FIXME#default 512

    enc = Zencoder(in_SEAN_channels, style_code_dim).to(img.device)
               
    ##first_out = masked_input          
    ##structure_model_output = self.structure_model(masked_input).clone() 
        
    ##print('clutter mask',clutter_mask.shape)
        
    ##sc_layout = get_segmentation_masks(tr_sem_layout)#####TO DO check if necessary
    
    ##print('Zenc input img',img.shape)##([1, 3, 512, 1024]) NB. must be the original source image - can be pre-computed only one time
    style_codes = enc(input=img, segmap=sem_layout)##([1, 3, 512])
    ##print('style_codes',style_codes.shape)

    return style_codes

class GatedNet(nn.Module):
    def __init__(self, device='none', backbone='none',full_size=False,decoder_type='none', inch = 18):#####NB. device here only for the SPADE option
        super(GatedNet, self).__init__()

        self.decoder_type = decoder_type

        self.backbone = backbone
                
        self.in_channels = inch##FIXME #### 3 + 1 mask
        self.out_channels = 3 ####rgb

        self.latent_channels = 64##default                            

        print('GatedNet using encoder',self.backbone)        

        if (self.backbone == 'light'):
            self.latent_channels = 32

        if (self.backbone == 'ultralight'):
            self.latent_channels = 16

        if (self.backbone == 'light_rgbs' or self.backbone == 'light_rgbe'):
            self.in_channels = 5 ####rgb+e+mask
            self.latent_channels = 32

        if (self.backbone == 'light_rgbde'):
            self.in_channels = 6 ####rgbd+e+mask
            self.latent_channels = 32

        if (self.backbone == 'decoupled_rgbe'):
            self.in_channels = 34
            self.latent_channels = 32

        if (self.backbone == 'light_rgbe_seg'):
            self.in_channels = 9 ####rgb+e+seg+mask
            self.latent_channels = 32


        if (self.decoder_type == 'rgbd' or self.decoder_type == 'rgbd_up'):
            self.out_channels = 4 ####rgbd

        if (self.backbone == 'light_rgb' and self.decoder_type == 'depth'):
            self.in_channels = 4 ####rgb+mask
            self.latent_channels = 32
            self.out_channels = 1 ####rgbd
                     
        if (self.decoder_type == 'rgb'):
            self.out_channels = 3 ####rgb

        self.pad_type = 'spherical'
        self.activation = 'relu'
        self.norm = 'in'

        self.stride1 = (2,2)####
        self.stride2 = (2,2)####

        if (self.backbone == 'rgbe_dr_sliced_sink'):
            self.in_channels = 5 ####rgb+e+mask
            self.latent_channels = 64
            self.pad_type = 'replicate'
            self.stride2 = (2,1)####

        if (self.backbone == 'rgbe_dr_sliced'):
            self.in_channels = 5 ####rgb+e+mask
            self.latent_channels = 64
            self.pad_type = 'replicate'
          

        if (self.backbone == 'light_rgbe_dr_sliced'):
            self.in_channels = 4 ####rgb+e+mask
            self.latent_channels = 32
            self.pad_type = 'replicate'
            ##self.pad_type = 'spherical'
            ##self.stride2 = (2,1)####            

                        
        self.use_sean = (self.decoder_type=='SEAN')

        print('using decoder:',self.decoder_type)

        self.sem_layout = None
        
        ####NOT USED - scaling is automatic
        self.full_size = full_size
        
        ################

        if (self.use_sean):
            ##SEMANTIC support 
            
            self.num_classes = 4 ##semantic classes
            self.in_SEAN_channels = 3 ####default 3 - rgb
            self.style_code_dim = 512 ###FIXME#default 512

            self.z_enc = Zencoder(self.in_SEAN_channels, self.style_code_dim)
            #
            #           
            self.in_spade_channels = 4 ####default 3 #####FIXME - get sem_layout channels

            m_factor = 2
                        
            self.spade_block_1 = SPADEResnetBlock(self.latent_channels*(2*m_factor), self.latent_channels*(2*m_factor), self.in_spade_channels, device=device, Block_Name='up_0')
            self.spade_block_2 = SPADEResnetBlock(self.latent_channels*m_factor, self.latent_channels*m_factor, self.in_spade_channels, device=device, Block_Name='up_1')
        
        self.refinement = nn.Sequential(
            # Surrounding Context Encoder
            GatedConv2d(self.in_channels, self.latent_channels, 5, self.stride1, 2, pad_type = self.pad_type, activation = self.activation, norm='none'),
            GatedConv2d(self.latent_channels, self.latent_channels * 2, 3, (1,1), 1, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 2, self.latent_channels * 4, 3, self.stride1, 1, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 1, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 1, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 1, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 2, dilation = 2, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 4, dilation = 4, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 8, dilation = 8, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 16, dilation = 16, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 1, pad_type = self.pad_type, activation = self.activation, norm = self.norm),
            GatedConv2d(self.latent_channels * 4, self.latent_channels * 4, 3, (1,1), 1, pad_type = self.pad_type, activation = self.activation), ###NB last encoder layer - eventually output mask
        )

        #Decoder
        self.refine_dec_1 = nn.Sequential(nn.Upsample(scale_factor=self.stride1),
        GatedConv2d(self.latent_channels * 4, self.latent_channels * 2, 3, (1,1), 1, activation = self.activation, pad_type='zero', norm=self.norm),
        )

        self.refine_dec_2 =  GatedConv2d(self.latent_channels * 2, self.latent_channels * 2, 3, (1,1), 1, pad_type = self.pad_type, activation = self.activation)
         
        if(self.backbone == 'rgbe_dr_sliced_sink'):
            self.refine_dec_3 = nn.Sequential(nn.Upsample(scale_factor=self.stride2),
            GatedConv2d(self.latent_channels * 2, self.latent_channels, 3, (1,1), 1, pad_type ='zero', activation = self.activation),
            )
        else:
            self.refine_dec_3 = nn.Sequential(nn.Upsample(scale_factor=self.stride1),
            GatedConv2d(self.latent_channels * 2, self.latent_channels, 3, (1,1), 1, pad_type ='zero', activation = self.activation),
            )

        if (self.decoder_type == 'rgbd' or self.decoder_type=='SEAN'):
            self.refine_dec_4 = GatedConv2d(self.latent_channels, 3, 3, (1,1), 1, pad_type = self.pad_type, norm='none', activation = 'tanh')
            self.refine_dec_4d = GatedConv2d(self.latent_channels, 1, 3, (1,1), 1, pad_type = self.pad_type, norm='none', activation = 'elu')

        if (self.decoder_type == 'none' or self.decoder_type == 'rgb'):
            self.refine_dec_4 = GatedConv2d(self.latent_channels, self.out_channels, 3, (1,1), 1, pad_type = self.pad_type, norm='none', activation = 'tanh')

        if (self.decoder_type == 'depth'):
            self.refine_dec_4d = GatedConv2d(self.latent_channels, 1, 3, (1,1), 1, pad_type = self.pad_type, norm='none', activation = 'elu')
                    
       

    def forward(self, img, occ_mask=None, masked_input=None, src_sem_layout = None, trg_sem_layout = None, style_codes = None): ####is our clutter mask ####NB. img is not used without SEAN
        
        if(self.backbone == 'light_rgbe_seg'):
            ##print(masked_input.shape, trg_sem_layout.shape)
            masked_seg_input = torch.cat((masked_input, trg_sem_layout), 1)
            second_out = self.refinement(torch.cat((masked_seg_input, occ_mask), 1))
        else:
            second_out = self.refinement(torch.cat((masked_input, occ_mask), 1))#####NB. clutter mask - masked_input is background + white
                            
        if (self.use_sean):

            if(style_codes == None):############NB. using learnable z_enc each forward ONLY for training
                ##print('using z_enc',img.shape, src_sem_layout.shape)
                ##style_codes = self.z_enc(input=img, segmap=src_sem_layout)#####input: Bx4xhxw classes
                style_codes = self.z_enc(input=img, segmap=trg_sem_layout)#####FIXME test with target

            assert(style_codes != None)
            assert(trg_sem_layout != None)

            ##print('sem_layout',sem_layout.shape,'style_codes',style_codes.shape)

            ##print('spade1 input features', second_out.shape)##([1, 128, 128, 256])
            second_out = self.spade_block_1(second_out, trg_sem_layout, style_codes)##([1, 128, 128, 256])
            ##print('z spade 1 out', second_out.shape)

            second_out = self.refine_dec_1(second_out)###upscale
            second_out = self.refine_dec_2(second_out)

            ##print('spade2 input features', second_out.shape)##([1, 64, 256, 512])
            second_out = self.spade_block_2(second_out, trg_sem_layout, style_codes)###([1, 64, 256, 512])
            ##print('z spade 2 out', second_out.shape)

            second_out = self.refine_dec_3(second_out)###upscale
            
            ##out = self.refine_dec_4(second_out)

            second_out1 = self.refine_dec_4(second_out)

            ##print(second_out1.shape)

            out2 = self.refine_dec_4d(second_out)
                                
            out1 = torch.clamp(second_out1, 0, 1)

            ##print(out1.shape,out2.shape)

            out = torch.cat([out1,out2],dim=1)
        else: 
            second_out = self.refine_dec_1(second_out)###upscale
            second_out = self.refine_dec_2(second_out)
            second_out = self.refine_dec_3(second_out)###upscale            

            if(self.decoder_type == 'rgbd'):

                ##print(second_out.shape)

                second_out1 = self.refine_dec_4(second_out)

                ##print(second_out1.shape)

                out2 = self.refine_dec_4d(second_out)
                                
                out1 = torch.clamp(second_out1, 0, 1)

                ##print(out1.shape,out2.shape)

                out = torch.cat([out1,out2],dim=1)

            if(self.decoder_type == 'none' or self.decoder_type == 'rgb'):
                ##second_out = self.refine_dec_4(second_out)
                second_out = self.refine_dec_4(second_out)
                out = torch.clamp(second_out, 0, 1)

            if(self.decoder_type == 'depth'):
                out = self.refine_dec_4d(second_out)

                            
        return out


def gatednet_counter():
    print('testing gatedNet image synth')

    ##os.environ['CUDA_VISIBLE_DEVICES'] = '3' ####FIXMEEEE

    device = torch.device('cuda')

    test_full = True

    d_type = 'depth'##'rgbd'###'SEAN'

    ##net = GatedNet(device, decoder_type=d_type, backbone ='light_rgb').to(device)
    net = GatedNet(device, decoder_type='rgbd', backbone ='light_rgbe').to(device)
    
    # testing
    layers = net

    if(test_full):
        inputs = []
        img = torch.randn(1, 3, 512, 1024).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 512, 1024).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 4, 512, 1024).to(device)
        inputs.append(masked_input)
    else:
        inputs = []
        img = torch.randn(1, 3, 256, 512).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 256, 512).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 4, 256, 512).to(device)
        inputs.append(masked_input)
        ##inputs.append(device)

    ##out = layers(img,mask,masked_input,device)

    with torch.no_grad():
        flops, params = profile(layers, inputs)
    ##print(f'input :', [v.shape for v in inputs])
    print(f'flops : {flops/(10**9):.2f} G')
    print(f'params: {params/(10**6):.2f} M')

    import time
    fps = []
    with torch.no_grad():
        out = layers(img,mask,masked_input)
        print('out shape',out.shape)
        
        for _ in range(50):
            eps_time = time.time()
            layers(img,mask,masked_input)
            torch.cuda.synchronize()
            eps_time = time.time() - eps_time
            fps.append(eps_time)
    print(f'fps   : {1 / (sum(fps) / len(fps)):.2f}')

def gatednet_depth_counter():
    print('testing gatedNet image synth')

    ##os.environ['CUDA_VISIBLE_DEVICES'] = '3' ####FIXMEEEE

    device = torch.device('cuda')

    test_full = True

    d_type = 'depth'##'rgbd'###'SEAN'

    net = GatedNet(device, decoder_type=d_type, backbone ='light_rgb').to(device)
    ##net = GatedNet(device, decoder_type='rgbd', backbone ='light_rgbe').to(device)
    
    # testing
    layers = net

    if(test_full):
        inputs = []
        img = torch.randn(1, 3, 512, 1024).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 512, 1024).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 3, 512, 1024).to(device)
        inputs.append(masked_input)
    else:
        inputs = []
        img = torch.randn(1, 3, 256, 512).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 256, 512).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 3, 256, 512).to(device)
        inputs.append(masked_input)
        ##inputs.append(device)

    ##out = layers(img,mask,masked_input,device)

    with torch.no_grad():
        flops, params = profile(layers, inputs)
    ##print(f'input :', [v.shape for v in inputs])
    print(f'flops : {flops/(10**9):.2f} G')
    print(f'params: {params/(10**6):.2f} M')

    import time
    fps = []
    with torch.no_grad():
        out = layers(img,mask,masked_input)
        print('out shape',out.shape)
        
        for _ in range(50):
            eps_time = time.time()
            layers(img,mask,masked_input)
            torch.cuda.synchronize()
            eps_time = time.time() - eps_time
            fps.append(eps_time)
    print(f'fps   : {1 / (sum(fps) / len(fps)):.2f}')

def segnet_counter():
    print('testing SegNet')

    device = torch.device('cuda')

    test_full = True

    net = SegNet(backbone ='light_depth').to(device)
    
    # testing
    layers = net

    if(test_full):
        inputs = []
        img = torch.randn(1, 1, 512, 512).to(device)#######FIXMEEEEE
        inputs.append(img)
        #
    else:
        inputs = []
        img = torch.randn(1, 1, 256, 256).to(device)
        inputs.append(img)
        
    ##out = layers(img,mask,masked_input,device)

    with torch.no_grad():
        flops, params = profile(layers, inputs)
    ##print(f'input :', [v.shape for v in inputs])
    print(f'flops : {flops/(10**9):.2f} G')
    print(f'params: {params/(10**6):.2f} M')

    import time
    fps = []
    with torch.no_grad():
        mask = layers(img)
        print('out shape', mask.shape)
        for _ in range(50):
            eps_time = time.time()
            layers(img)
            torch.cuda.synchronize()
            eps_time = time.time() - eps_time
            fps.append(eps_time)
    print(f'fps   : {1 / (sum(fps) / len(fps)):.2f}')

def sean_counter():
    print('testing gatedNet image synth')

    ##os.environ['CUDA_VISIBLE_DEVICES'] = '3' ####FIXMEEEE

    device = torch.device('cuda')

    test_full = True

    d_type = 'SEAN'

    net = GatedNet(device, decoder_type=d_type, backbone ='light_rgbe').to(device)
    ##net = GatedNet(device, decoder_type='rgbd', backbone ='light_rgbe_seg').to(device)

        
    # testing
    layers = net

    if(test_full):
        inputs = []
        img = torch.randn(1, 3, 512, 1024).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 512, 1024).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 4, 512, 1024).to(device)
        inputs.append(masked_input)
    else:
        inputs = []
        img = torch.randn(1, 3, 256, 512).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 256, 512).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 4, 256, 512).to(device)
        inputs.append(masked_input)
        ##inputs.append(device)

    ####SEAN support
    num_classes = 4
    sem_layout = torch.zeros((img.shape[0], num_classes, img.shape[2], img.shape[3])).float().to(device)##FIXME

    ##style_codes = get_z_encoding(img,sem_layout,device)####NB. here style codes are computed on the src pose

    inputs.append(sem_layout)###src
    inputs.append(sem_layout)###trg
    ##inputs.append(style_codes)


    ##out = layers(img,mask,masked_input,device)

    with torch.no_grad():
        flops, params = profile(layers, inputs)
    ##print(f'input :', [v.shape for v in inputs])
    print(f'flops : {flops/(10**9):.2f} G')
    print(f'params: {params/(10**6):.2f} M')

    import time
    fps = []
    with torch.no_grad():
        out = layers(img,mask,masked_input,sem_layout,sem_layout)##,style_codes)
        print('out shape',out.shape)
        
        for _ in range(50):
            eps_time = time.time()
            layers(img,mask,masked_input,sem_layout,sem_layout)##,style_codes)
            torch.cuda.synchronize()
            eps_time = time.time() - eps_time
            fps.append(eps_time)
    print(f'fps   : {1 / (sum(fps) / len(fps)):.2f}')

def gatednet_seg_counter():
    print('testing gatedNet image synth')

    ##os.environ['CUDA_VISIBLE_DEVICES'] = '3' ####FIXMEEEE

    device = torch.device('cuda')

    test_full = True

    d_type = 'SEAN'

    ##net = GatedNet(device, decoder_type=d_type, backbone ='light_rgbe').to(device)
    net = GatedNet(device, decoder_type='rgbd', backbone ='light_rgbe_seg').to(device)

        
    # testing
    layers = net

    if(test_full):
        inputs = []
        img = torch.randn(1, 3, 512, 1024).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 512, 1024).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 4, 512, 1024).to(device)
        inputs.append(masked_input)
    else:
        inputs = []
        img = torch.randn(1, 3, 256, 512).to(device)
        inputs.append(img)
        mask = torch.randn(1, 1, 256, 512).to(device)
        inputs.append(mask)
        masked_input = torch.randn(1, 4, 256, 512).to(device)
        inputs.append(masked_input)
        ##inputs.append(device)

    ####SEAN support
    num_classes = 4
    sem_layout = torch.zeros((img.shape[0], num_classes, img.shape[2], img.shape[3])).float().to(device)##FIXME

    ##style_codes = get_z_encoding(img,sem_layout,device)####NB. here style codes are computed on the src pose

    inputs.append(sem_layout)###src
    inputs.append(sem_layout)###trg
    ##inputs.append(style_codes)


    ##out = layers(img,mask,masked_input,device)

    with torch.no_grad():
        flops, params = profile(layers, inputs)
    ##print(f'input :', [v.shape for v in inputs])
    print(f'flops : {flops/(10**9):.2f} G')
    print(f'params: {params/(10**6):.2f} M')

    import time
    fps = []
    with torch.no_grad():
        out = layers(img,mask,masked_input,sem_layout,sem_layout)##,style_codes)
        print('out shape',out.shape)
        
        for _ in range(50):
            eps_time = time.time()
            layers(img,mask,masked_input,sem_layout,sem_layout)##,style_codes)
            torch.cuda.synchronize()
            eps_time = time.time() - eps_time
            fps.append(eps_time)
    print(f'fps   : {1 / (sum(fps) / len(fps)):.2f}')

if __name__ == '__main__':
    ##gatednet_counter()
    ##gatednet_depth_counter()
    segnet_counter()
    ##sean_counter()
    ##gatednet_seg_counter()
