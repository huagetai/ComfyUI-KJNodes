import nodes
import torch
import torch.nn.functional as F
import torchvision.utils as vutils
import scipy.ndimage
import numpy as np
from PIL import ImageFilter, Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo
import json
import re
import os
import librosa
from scipy.special import erf
from .fluid import Fluid
import comfy.model_management
import math
from nodes import MAX_RESOLUTION
import folder_paths
from comfy.cli_args import args

script_dir = os.path.dirname(os.path.abspath(__file__))

class INTConstant:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "value": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
        },
        }

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("value",)
    FUNCTION = "get_value"

    CATEGORY = "KJNodes"

    def get_value(self, value):
        return (value,)

class FloatConstant:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "value": ("FLOAT", {"default": 0.0, "min": -0xffffffffffffffff, "max": 0xffffffffffffffff, "step": 0.01}),
        },
        }

    RETURN_TYPES = ("FLOAT",)
    RETURN_NAMES = ("value",)
    FUNCTION = "get_value"

    CATEGORY = "KJNodes"

    def get_value(self, value):
        return (value,)

def gaussian_kernel(kernel_size: int, sigma: float, device=None):
        x, y = torch.meshgrid(torch.linspace(-1, 1, kernel_size, device=device), torch.linspace(-1, 1, kernel_size, device=device), indexing="ij")
        d = torch.sqrt(x * x + y * y)
        g = torch.exp(-(d * d) / (2.0 * sigma * sigma))
        return g / g.sum()

class CreateFluidMask:
    
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "createfluidmask"
    CATEGORY = "KJNodes/masking/generate"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "invert": ("BOOLEAN", {"default": False}),
                 "frames": ("INT", {"default": 0,"min": 0, "max": 255, "step": 1}),
                 "width": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
                 "height": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
                 "inflow_count": ("INT", {"default": 3,"min": 0, "max": 255, "step": 1}),
                 "inflow_velocity": ("INT", {"default": 1,"min": 0, "max": 255, "step": 1}),
                 "inflow_radius": ("INT", {"default": 8,"min": 0, "max": 255, "step": 1}),
                 "inflow_padding": ("INT", {"default": 50,"min": 0, "max": 255, "step": 1}),
                 "inflow_duration": ("INT", {"default": 60,"min": 0, "max": 255, "step": 1}),

        },
    } 
    #using code from https://github.com/GregTJ/stable-fluids
    def createfluidmask(self, frames, width, height, invert, inflow_count, inflow_velocity, inflow_radius, inflow_padding, inflow_duration):
        out = []
        masks = []
        RESOLUTION = width, height
        DURATION = frames

        INFLOW_PADDING = inflow_padding
        INFLOW_DURATION = inflow_duration
        INFLOW_RADIUS = inflow_radius
        INFLOW_VELOCITY = inflow_velocity
        INFLOW_COUNT = inflow_count

        print('Generating fluid solver, this may take some time.')
        fluid = Fluid(RESOLUTION, 'dye')

        center = np.floor_divide(RESOLUTION, 2)
        r = np.min(center) - INFLOW_PADDING

        points = np.linspace(-np.pi, np.pi, INFLOW_COUNT, endpoint=False)
        points = tuple(np.array((np.cos(p), np.sin(p))) for p in points)
        normals = tuple(-p for p in points)
        points = tuple(r * p + center for p in points)

        inflow_velocity = np.zeros_like(fluid.velocity)
        inflow_dye = np.zeros(fluid.shape)
        for p, n in zip(points, normals):
            mask = np.linalg.norm(fluid.indices - p[:, None, None], axis=0) <= INFLOW_RADIUS
            inflow_velocity[:, mask] += n[:, None] * INFLOW_VELOCITY
            inflow_dye[mask] = 1

        
        for f in range(DURATION):
            print(f'Computing frame {f + 1} of {DURATION}.')
            if f <= INFLOW_DURATION:
                fluid.velocity += inflow_velocity
                fluid.dye += inflow_dye

            curl = fluid.step()[1]
            # Using the error function to make the contrast a bit higher. 
            # Any other sigmoid function e.g. smoothstep would work.
            curl = (erf(curl * 2) + 1) / 4

            color = np.dstack((curl, np.ones(fluid.shape), fluid.dye))
            color = (np.clip(color, 0, 1) * 255).astype('uint8')
            image = np.array(color).astype(np.float32) / 255.0
            image = torch.from_numpy(image)[None,]
            mask = image[:, :, :, 0] 
            masks.append(mask)
            out.append(image)
        
        if invert:
            return (1.0 - torch.cat(out, dim=0),1.0 - torch.cat(masks, dim=0),)
        return (torch.cat(out, dim=0),torch.cat(masks, dim=0),)

class CreateAudioMask:
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "createaudiomask"
    CATEGORY = "KJNodes/masking/generate"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "invert": ("BOOLEAN", {"default": False}),
                 "frames": ("INT", {"default": 0,"min": 0, "max": 255, "step": 1}),
                 "scale": ("FLOAT", {"default": 0.5,"min": 0.0, "max": 2.0, "step": 0.01}),
                 "audio_path": ("STRING", {"default": "audio.wav"}),
                 "width": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
                 "height": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
        },
    } 

    def createaudiomask(self, frames, width, height, invert, audio_path, scale):
             # Define the number of images in the batch
        batch_size = frames
        out = []
        masks = []
        if audio_path == "audio.wav": #I don't know why relative path won't work otherwise...
            audio_path = os.path.join(script_dir, audio_path)
        audio, sr = librosa.load(audio_path)
        spectrogram = np.abs(librosa.stft(audio))
        
        for i in range(batch_size):
           image = Image.new("RGB", (width, height), "black")
           draw = ImageDraw.Draw(image)
           frame = spectrogram[:, i]
           circle_radius = int(height * np.mean(frame))
           circle_radius *= scale
           circle_center = (width // 2, height // 2)  # Calculate the center of the image

           draw.ellipse([(circle_center[0] - circle_radius, circle_center[1] - circle_radius),
                      (circle_center[0] + circle_radius, circle_center[1] + circle_radius)],
                      fill='white')
             
           image = np.array(image).astype(np.float32) / 255.0
           image = torch.from_numpy(image)[None,]
           mask = image[:, :, :, 0] 
           masks.append(mask)
           out.append(image)

        if invert:
            return (1.0 - torch.cat(out, dim=0),)
        return (torch.cat(out, dim=0),torch.cat(masks, dim=0),)
       

    
class CreateGradientMask:
    
    RETURN_TYPES = ("MASK",)
    FUNCTION = "createmask"
    CATEGORY = "KJNodes/masking/generate"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "invert": ("BOOLEAN", {"default": False}),
                 "frames": ("INT", {"default": 0,"min": 0, "max": 255, "step": 1}),
                 "width": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
                 "height": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
        },
    } 
    def createmask(self, frames, width, height, invert):
        # Define the number of images in the batch
        batch_size = frames
        out = []
        # Create an empty array to store the image batch
        image_batch = np.zeros((batch_size, height, width), dtype=np.float32)
        # Generate the black to white gradient for each image
        for i in range(batch_size):
            gradient = np.linspace(1.0, 0.0, width, dtype=np.float32)
            time = i / frames  # Calculate the time variable
            offset_gradient = gradient - time  # Offset the gradient values based on time
            image_batch[i] = offset_gradient.reshape(1, -1)
        output = torch.from_numpy(image_batch)
        mask = output
        out.append(mask)
        if invert:
            return (1.0 - torch.cat(out, dim=0),)
        return (torch.cat(out, dim=0),)

class CreateFadeMask:
    
    RETURN_TYPES = ("MASK",)
    FUNCTION = "createfademask"
    CATEGORY = "KJNodes/masking/generate"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "invert": ("BOOLEAN", {"default": False}),
                 "frames": ("INT", {"default": 0,"min": 0, "max": 255, "step": 1}),
                 "width": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
                 "height": ("INT", {"default": 256,"min": 16, "max": 4096, "step": 1}),
                 "interpolation": (["linear", "ease_in", "ease_out", "ease_in_out"],),
                 "start_level": ("FLOAT", {"default": 1.0,"min": 0.0, "max": 1.0, "step": 0.01}),
                 "midpoint_level": ("FLOAT", {"default": 0.5,"min": 0.0, "max": 1.0, "step": 0.01}),
                 "end_level": ("FLOAT", {"default": 0.0,"min": 0.0, "max": 1.0, "step": 0.01}),
        },
    } 
    
    def createfademask(self, frames, width, height, invert, interpolation, start_level, midpoint_level, end_level):
        def ease_in(t):
            return t * t

        def ease_out(t):
            return 1 - (1 - t) * (1 - t)

        def ease_in_out(t):
            return 3 * t * t - 2 * t * t * t

        batch_size = frames
        out = []
        image_batch = np.zeros((batch_size, height, width), dtype=np.float32)
        
        for i in range(batch_size):
            t = i / (batch_size - 1)
            
            if interpolation == "ease_in":
                t = ease_in(t)
            elif interpolation == "ease_out":
                t = ease_out(t)
            elif interpolation == "ease_in_out":
                t = ease_in_out(t)
            
            if midpoint_level is not None:
                if t < 0.5:
                    color = start_level - t * (start_level - midpoint_level) * 2
                else:
                    color = midpoint_level - (t - 0.5) * (midpoint_level - end_level) * 2
            else:
                color = start_level - t * (start_level - end_level)
            
            image = np.full((height, width), color, dtype=np.float32)
            image_batch[i] = image
            
        output = torch.from_numpy(image_batch)
        mask = output
        out.append(mask)
        
        if invert:
            return (1.0 - torch.cat(out, dim=0),)
        return (torch.cat(out, dim=0),)

class CrossFadeImages:
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "crossfadeimages"
    CATEGORY = "KJNodes"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "images_1": ("IMAGE",),
                 "images_2": ("IMAGE",),
                 "interpolation": (["linear", "ease_in", "ease_out", "ease_in_out", "bounce", "elastic", "glitchy", "exponential_ease_out"],),
                 "transition_start_index": ("INT", {"default": 1,"min": 0, "max": 4096, "step": 1}),
                 "transitioning_frames": ("INT", {"default": 1,"min": 0, "max": 4096, "step": 1}),
                 "start_level": ("FLOAT", {"default": 0.0,"min": 0.0, "max": 1.0, "step": 0.01}),
                 "end_level": ("FLOAT", {"default": 1.0,"min": 0.0, "max": 1.0, "step": 0.01}),
        },
    } 
    
    def crossfadeimages(self, images_1, images_2, transition_start_index, transitioning_frames, interpolation, start_level, end_level):

        def crossfade(images_1, images_2, alpha):
            crossfade = (1 - alpha) * images_1 + alpha * images_2
            return crossfade
        def ease_in(t):
            return t * t
        def ease_out(t):
            return 1 - (1 - t) * (1 - t)
        def ease_in_out(t):
            return 3 * t * t - 2 * t * t * t
        def bounce(t):
            if t < 0.5:
                return self.ease_out(t * 2) * 0.5
            else:
                return self.ease_in((t - 0.5) * 2) * 0.5 + 0.5
        def elastic(t):
            return math.sin(13 * math.pi / 2 * t) * math.pow(2, 10 * (t - 1))
        def glitchy(t):
            return t + 0.1 * math.sin(40 * t)
        def exponential_ease_out(t):
            return 1 - (1 - t) ** 4

        easing_functions = {
            "linear": lambda t: t,
            "ease_in": ease_in,
            "ease_out": ease_out,
            "ease_in_out": ease_in_out,
            "bounce": bounce,
            "elastic": elastic,
            "glitchy": glitchy,
            "exponential_ease_out": exponential_ease_out,
        }

        crossfade_images = []

        alphas = torch.linspace(start_level, end_level, transitioning_frames)
        for i in range(transitioning_frames):
            alpha = alphas[i]
            image1 = images_1[i + transition_start_index]
            image2 = images_2[i + transition_start_index]
            easing_function = easing_functions.get(interpolation)
            alpha = easing_function(alpha)  # Apply the easing function to the alpha value

            crossfade_image = crossfade(image1, image2, alpha)
            crossfade_images.append(crossfade_image)
            
        # Convert crossfade_images to tensor
        crossfade_images = torch.stack(crossfade_images, dim=0)
        # Get the last frame result of the interpolation
        last_frame = crossfade_images[-1]
        # Calculate the number of remaining frames from images_2
        remaining_frames = len(images_2) - (transition_start_index + transitioning_frames)
        # Crossfade the remaining frames with the last used alpha value
        for i in range(remaining_frames):
            alpha = alphas[-1]
            image1 = images_1[i + transition_start_index + transitioning_frames]
            image2 = images_2[i + transition_start_index + transitioning_frames]
            easing_function = easing_functions.get(interpolation)
            alpha = easing_function(alpha)  # Apply the easing function to the alpha value

            crossfade_image = crossfade(image1, image2, alpha)
            crossfade_images = torch.cat([crossfade_images, crossfade_image.unsqueeze(0)], dim=0)
        # Append the beginning of images_1
        beginning_images_1 = images_1[:transition_start_index]
        crossfade_images = torch.cat([beginning_images_1, crossfade_images], dim=0)
        return (crossfade_images, )

class GetImageRangeFromBatch:
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "imagesfrombatch"
    CATEGORY = "KJNodes"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "images": ("IMAGE",),
                 "start_index": ("INT", {"default": 1,"min": 0, "max": 4096, "step": 1}),
                 "num_frames": ("INT", {"default": 1,"min": 0, "max": 4096, "step": 1}),
        },
    } 
    
    def imagesfrombatch(self, images, start_index, num_frames):
        if start_index >= len(images):
            raise ValueError("GetImageRangeFromBatch: Start index is out of range")
        end_index = start_index + num_frames
        if end_index > len(images):
            raise ValueError("GetImageRangeFromBatch: End index is out of range")
        chosen_images = images[start_index:end_index]
        return (chosen_images, )
  
class ReplaceImagesInBatch:
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "replace"
    CATEGORY = "KJNodes"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "original_images": ("IMAGE",),
                 "replacement_images": ("IMAGE",),
                 "start_index": ("INT", {"default": 1,"min": 0, "max": 4096, "step": 1}),
        },
    } 
    
    def replace(self, original_images, replacement_images, start_index):
        images = None
        if start_index >= len(original_images):
            raise ValueError("GetImageRangeFromBatch: Start index is out of range")
        end_index = start_index + len(replacement_images)
        if end_index > len(original_images):
            raise ValueError("GetImageRangeFromBatch: End index is out of range")
         # Create a copy of the original_images tensor
        original_images_copy = original_images.clone()
        original_images_copy[start_index:end_index] = replacement_images
        images = original_images_copy
        return (images, )
    

class ReverseImageBatch:
    
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "reverseimagebatch"
    CATEGORY = "KJNodes"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "images": ("IMAGE",),
        },
    } 
    
    def reverseimagebatch(self, images):
        reversed_images = torch.flip(images, [0])
        return (reversed_images, )
    
class CreateTextMask:
    
    RETURN_TYPES = ("IMAGE", "MASK",)
    FUNCTION = "createtextmask"
    CATEGORY = "KJNodes/masking/generate"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "invert": ("BOOLEAN", {"default": False}),
                 "frames": ("INT", {"default": 1,"min": 1, "max": 4096, "step": 1}),
                 "text_x": ("INT", {"default": 0,"min": 0, "max": 4096, "step": 1}),
                 "text_y": ("INT", {"default": 0,"min": 0, "max": 4096, "step": 1}),
                 "font_size": ("INT", {"default": 32,"min": 8, "max": 4096, "step": 1}),
                 "font_color": ("STRING", {"default": "white"}),
                 "text": ("STRING", {"default": "HELLO!"}),
                 "font_path": ("STRING", {"default": "fonts\\TTNorms-Black.otf"}),
                 "width": ("INT", {"default": 512,"min": 16, "max": 4096, "step": 1}),
                 "height": ("INT", {"default": 512,"min": 16, "max": 4096, "step": 1}),
                 "start_rotation": ("INT", {"default": 0,"min": 0, "max": 359, "step": 1}),
                 "end_rotation": ("INT", {"default": 0,"min": -359, "max": 359, "step": 1}),
        },
    } 

    def createtextmask(self, frames, width, height, invert, text_x, text_y, text, font_size, font_color, font_path, start_rotation, end_rotation):
        # Define the number of images in the batch
        batch_size = frames
        out = []
        masks = []
        rotation = start_rotation
        if start_rotation != end_rotation:
            rotation_increment = (end_rotation - start_rotation) / (batch_size - 1)
        if font_path == "fonts\\TTNorms-Black.otf": #I don't know why relative path won't work otherwise...
            font_path = os.path.join(script_dir, font_path)
        # Generate the text
        for i in range(batch_size):
            image = Image.new("RGB", (width, height), "black")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(font_path, font_size)
            text_width = font.getlength(text)
            text_height = font_size
            text_center_x = text_x + text_width / 2
            text_center_y = text_y + text_height / 2
            draw.text((text_x, text_y), text, font=font, fill=font_color)
            if start_rotation != end_rotation:
                image = image.rotate(rotation, center=(text_center_x, text_center_y))
                rotation += rotation_increment
            image = np.array(image).astype(np.float32) / 255.0
            image = torch.from_numpy(image)[None,]
            mask = image[:, :, :, 0] 
            masks.append(mask)
            out.append(image)
            
        if invert:
            return (1.0 - torch.cat(out, dim=0), 1.0 - torch.cat(masks, dim=0),)
        return (torch.cat(out, dim=0),torch.cat(masks, dim=0),)
    
class GrowMaskWithBlur:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mask": ("MASK",),
                "expand": ("INT", {"default": 0, "min": -MAX_RESOLUTION, "max": MAX_RESOLUTION, "step": 1}),
                "incremental_expandrate": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1}),
                "tapered_corners": ("BOOLEAN", {"default": True}),
                "flip_input": ("BOOLEAN", {"default": False}),
                "use_cuda": ("BOOLEAN", {"default": True}),
                "blur_radius": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 999,
                    "step": 1
                }),
                "sigma": ("FLOAT", {
                    "default": 1.0,
                    "min": 0.1,
                    "max": 10.0,
                    "step": 0.1
                }),
            },
        }
    
    CATEGORY = "KJNodes/masking"

    RETURN_TYPES = ("MASK", "MASK",)
    RETURN_NAMES = ("mask", "mask_inverted",)
    FUNCTION = "expand_mask"
    
    def expand_mask(self, mask, expand, tapered_corners, flip_input, blur_radius, sigma, incremental_expandrate, use_cuda):
        if( flip_input ):
            mask = 1.0 - mask
        c = 0 if tapered_corners else 1
        kernel = np.array([[c, 1, c],
                           [1, 1, 1],
                           [c, 1, c]])
        growmask = mask.reshape((-1, mask.shape[-2], mask.shape[-1]))
        out = []
        for m in growmask:
            output = m.numpy()
            for _ in range(abs(expand)):
                if expand < 0:
                    output = scipy.ndimage.grey_erosion(output, footprint=kernel)
                else:
                    output = scipy.ndimage.grey_dilation(output, footprint=kernel)
            if expand < 0:
                expand -= abs(incremental_expandrate)  # Use abs(growrate) to ensure positive change
            else:
                expand += abs(incremental_expandrate)  # Use abs(growrate) to ensure positive change
            output = torch.from_numpy(output)
            out.append(output)

        blurred = torch.stack(out, dim=0).reshape((-1, 1, mask.shape[-2], mask.shape[-1])).movedim(1, -1).expand(-1, -1, -1, 3)
        if use_cuda:    
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            blurred = blurred.to(device)  # Move blurred tensor to the GPU

        batch_size, height, width, channels = blurred.shape
        if blur_radius != 0:
            blurkernel_size = blur_radius * 2 + 1
            blurkernel = gaussian_kernel(blurkernel_size, sigma, device=blurred.device).repeat(channels, 1, 1).unsqueeze(1)
            blurred = blurred.permute(0, 3, 1, 2) # Torch wants (B, C, H, W) we use (B, H, W, C)
            padded_image = F.pad(blurred, (blur_radius,blur_radius,blur_radius,blur_radius), 'reflect')
            blurred = F.conv2d(padded_image, blurkernel, padding=blurkernel_size // 2, groups=channels)[:,:,blur_radius:-blur_radius, blur_radius:-blur_radius]
            blurred = blurred.permute(0, 2, 3, 1)
            blurred = blurred[:, :, :, 0]        
            return (blurred, 1.0 - blurred,)
        return (torch.stack(out, dim=0), 1.0 -torch.stack(out, dim=0),)
           
        
        
class PlotNode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "start": ("FLOAT", {"default": 0.5, "min": 0.5, "max": 1.0}),
            "max_frames": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
        }}

    RETURN_TYPES = ("FLOAT", "INT",)
    FUNCTION = "plot"
    CATEGORY = "KJNodes"

    def plot(self, start, max_frames):
        result = start + max_frames
        return (result,)

class ColorToMask:
    
    RETURN_TYPES = ("MASK",)
    FUNCTION = "clip"
    CATEGORY = "KJNodes/masking"

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                 "images": ("IMAGE",),
                 "invert": ("BOOLEAN", {"default": False}),
                 "red": ("INT", {"default": 0,"min": 0, "max": 255, "step": 1}),
                 "green": ("INT", {"default": 0,"min": 0, "max": 255, "step": 1}),
                 "blue": ("INT", {"default": 0,"min": 0, "max": 255, "step": 1}),
                 "threshold": ("INT", {"default": 10,"min": 0, "max": 255, "step": 1}),
        },
    } 

    def clip(self, images, red, green, blue, threshold, invert):
        color = np.array([red, green, blue])
        images = 255. * images.cpu().numpy()
        images = np.clip(images, 0, 255).astype(np.uint8)
        images = [Image.fromarray(image) for image in images]
        images = [np.array(image) for image in images]

        black = [0, 0, 0]
        white = [255, 255, 255]
        if invert:
             black, white = white, black

        new_images = []
        for image in images:
            new_image = np.full_like(image, black)

            color_distances = np.linalg.norm(image - color, axis=-1)
            complement_indexes = color_distances <= threshold

            new_image[complement_indexes] = white

            new_images.append(new_image)

        new_images = np.array(new_images).astype(np.float32) / 255.0
        new_images = torch.from_numpy(new_images).permute(3, 0, 1, 2)
        return new_images
      
class ConditioningMultiCombine:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "inputcount": ("INT", {"default": 2, "min": 2, "max": 20, "step": 1}),
                "conditioning_1": ("CONDITIONING", ),
                "conditioning_2": ("CONDITIONING", ),
            },
        
    }

    RETURN_TYPES = ("CONDITIONING", "INT")
    RETURN_NAMES = ("combined", "inputcount")
    FUNCTION = "combine"
    CATEGORY = "KJNodes/masking/conditioning"

    def combine(self, inputcount, **kwargs):
        cond_combine_node = nodes.ConditioningCombine()
        cond = kwargs["conditioning_1"]
        for c in range(1, inputcount):
            new_cond = kwargs[f"conditioning_{c + 1}"]
            cond = cond_combine_node.combine(new_cond, cond)[0]
        return (cond, inputcount,)

def append_helper(t, mask, c, set_area_to_bounds, strength):
        n = [t[0], t[1].copy()]
        _, h, w = mask.shape
        n[1]['mask'] = mask
        n[1]['set_area_to_bounds'] = set_area_to_bounds
        n[1]['mask_strength'] = strength
        c.append(n)  

class ConditioningSetMaskAndCombine:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive_1": ("CONDITIONING", ),
                "negative_1": ("CONDITIONING", ),
                "positive_2": ("CONDITIONING", ),
                "negative_2": ("CONDITIONING", ),
                "mask_1": ("MASK", ),
                "mask_2": ("MASK", ),
                "mask_1_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_2_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "set_cond_area": (["default", "mask bounds"],),
            }
        }

    RETURN_TYPES = ("CONDITIONING","CONDITIONING",)
    RETURN_NAMES = ("combined_positive", "combined_negative",)
    FUNCTION = "append"
    CATEGORY = "KJNodes/masking/conditioning"

    def append(self, positive_1, negative_1, positive_2, negative_2, mask_1, mask_2, set_cond_area, mask_1_strength, mask_2_strength):
        c = []
        c2 = []
        set_area_to_bounds = False
        if set_cond_area != "default":
            set_area_to_bounds = True
        if len(mask_1.shape) < 3:
            mask_1 = mask_1.unsqueeze(0)
        if len(mask_2.shape) < 3:
            mask_2 = mask_2.unsqueeze(0)
        for t in positive_1:
            append_helper(t, mask_1, c, set_area_to_bounds, mask_1_strength)
        for t in positive_2:
            append_helper(t, mask_2, c, set_area_to_bounds, mask_2_strength)
        for t in negative_1:
            append_helper(t, mask_1, c2, set_area_to_bounds, mask_1_strength)
        for t in negative_2:
            append_helper(t, mask_2, c2, set_area_to_bounds, mask_2_strength)
        return (c, c2)

class ConditioningSetMaskAndCombine3:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive_1": ("CONDITIONING", ),
                "negative_1": ("CONDITIONING", ),
                "positive_2": ("CONDITIONING", ),
                "negative_2": ("CONDITIONING", ),
                "positive_3": ("CONDITIONING", ),
                "negative_3": ("CONDITIONING", ),
                "mask_1": ("MASK", ),
                "mask_2": ("MASK", ),
                "mask_3": ("MASK", ),
                "mask_1_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_2_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_3_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "set_cond_area": (["default", "mask bounds"],),
            }
        }

    RETURN_TYPES = ("CONDITIONING","CONDITIONING",)
    RETURN_NAMES = ("combined_positive", "combined_negative",)
    FUNCTION = "append"
    CATEGORY = "KJNodes/masking/conditioning"

    def append(self, positive_1, negative_1, positive_2, positive_3, negative_2, negative_3, mask_1, mask_2, mask_3, set_cond_area, mask_1_strength, mask_2_strength, mask_3_strength):
        c = []
        c2 = []
        set_area_to_bounds = False
        if set_cond_area != "default":
            set_area_to_bounds = True
        if len(mask_1.shape) < 3:
            mask_1 = mask_1.unsqueeze(0)
        if len(mask_2.shape) < 3:
            mask_2 = mask_2.unsqueeze(0)
        if len(mask_3.shape) < 3:
            mask_3 = mask_3.unsqueeze(0)
        for t in positive_1:
            append_helper(t, mask_1, c, set_area_to_bounds, mask_1_strength)
        for t in positive_2:
            append_helper(t, mask_2, c, set_area_to_bounds, mask_2_strength)
        for t in positive_3:
            append_helper(t, mask_3, c, set_area_to_bounds, mask_3_strength)
        for t in negative_1:
            append_helper(t, mask_1, c2, set_area_to_bounds, mask_1_strength)
        for t in negative_2:
            append_helper(t, mask_2, c2, set_area_to_bounds, mask_2_strength)
        for t in negative_3:
            append_helper(t, mask_3, c2, set_area_to_bounds, mask_3_strength)
        return (c, c2)

class ConditioningSetMaskAndCombine4:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive_1": ("CONDITIONING", ),
                "negative_1": ("CONDITIONING", ),
                "positive_2": ("CONDITIONING", ),
                "negative_2": ("CONDITIONING", ),
                "positive_3": ("CONDITIONING", ),
                "negative_3": ("CONDITIONING", ),
                "positive_4": ("CONDITIONING", ),
                "negative_4": ("CONDITIONING", ),
                "mask_1": ("MASK", ),
                "mask_2": ("MASK", ),
                "mask_3": ("MASK", ),
                "mask_4": ("MASK", ),
                "mask_1_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_2_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_3_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_4_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "set_cond_area": (["default", "mask bounds"],),
            }
        }

    RETURN_TYPES = ("CONDITIONING","CONDITIONING",)
    RETURN_NAMES = ("combined_positive", "combined_negative",)
    FUNCTION = "append"
    CATEGORY = "KJNodes/masking/conditioning"

    def append(self, positive_1, negative_1, positive_2, positive_3, positive_4, negative_2, negative_3, negative_4, mask_1, mask_2, mask_3, mask_4, set_cond_area, mask_1_strength, mask_2_strength, mask_3_strength, mask_4_strength):
        c = []
        c2 = []
        set_area_to_bounds = False
        if set_cond_area != "default":
            set_area_to_bounds = True
        if len(mask_1.shape) < 3:
            mask_1 = mask_1.unsqueeze(0)
        if len(mask_2.shape) < 3:
            mask_2 = mask_2.unsqueeze(0)
        if len(mask_3.shape) < 3:
            mask_3 = mask_3.unsqueeze(0)
        if len(mask_4.shape) < 3:
            mask_4 = mask_4.unsqueeze(0)
        for t in positive_1:
            append_helper(t, mask_1, c, set_area_to_bounds, mask_1_strength)
        for t in positive_2:
            append_helper(t, mask_2, c, set_area_to_bounds, mask_2_strength)
        for t in positive_3:
            append_helper(t, mask_3, c, set_area_to_bounds, mask_3_strength)
        for t in positive_4:
            append_helper(t, mask_4, c, set_area_to_bounds, mask_4_strength)
        for t in negative_1:
            append_helper(t, mask_1, c2, set_area_to_bounds, mask_1_strength)
        for t in negative_2:
            append_helper(t, mask_2, c2, set_area_to_bounds, mask_2_strength)
        for t in negative_3:
            append_helper(t, mask_3, c2, set_area_to_bounds, mask_3_strength)
        for t in negative_4:
            append_helper(t, mask_4, c2, set_area_to_bounds, mask_4_strength)
        return (c, c2)

class ConditioningSetMaskAndCombine5:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "positive_1": ("CONDITIONING", ),
                "negative_1": ("CONDITIONING", ),
                "positive_2": ("CONDITIONING", ),
                "negative_2": ("CONDITIONING", ),
                "positive_3": ("CONDITIONING", ),
                "negative_3": ("CONDITIONING", ),
                "positive_4": ("CONDITIONING", ),
                "negative_4": ("CONDITIONING", ),
                "positive_5": ("CONDITIONING", ),
                "negative_5": ("CONDITIONING", ),
                "mask_1": ("MASK", ),
                "mask_2": ("MASK", ),
                "mask_3": ("MASK", ),
                "mask_4": ("MASK", ),
                "mask_5": ("MASK", ),
                "mask_1_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_2_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_3_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_4_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "mask_5_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "set_cond_area": (["default", "mask bounds"],),
            }
        }

    RETURN_TYPES = ("CONDITIONING","CONDITIONING",)
    RETURN_NAMES = ("combined_positive", "combined_negative",)
    FUNCTION = "append"
    CATEGORY = "KJNodes/masking/conditioning"

    def append(self, positive_1, negative_1, positive_2, positive_3, positive_4, positive_5, negative_2, negative_3, negative_4, negative_5, mask_1, mask_2, mask_3, mask_4, mask_5, set_cond_area, mask_1_strength, mask_2_strength, mask_3_strength, mask_4_strength, mask_5_strength):
        c = []
        c2 = []
        set_area_to_bounds = False
        if set_cond_area != "default":
            set_area_to_bounds = True
        if len(mask_1.shape) < 3:
            mask_1 = mask_1.unsqueeze(0)
        if len(mask_2.shape) < 3:
            mask_2 = mask_2.unsqueeze(0)
        if len(mask_3.shape) < 3:
            mask_3 = mask_3.unsqueeze(0)
        if len(mask_4.shape) < 3:
            mask_4 = mask_4.unsqueeze(0)
        if len(mask_5.shape) < 3:
            mask_5 = mask_5.unsqueeze(0)
        for t in positive_1:
            append_helper(t, mask_1, c, set_area_to_bounds, mask_1_strength)
        for t in positive_2:
            append_helper(t, mask_2, c, set_area_to_bounds, mask_2_strength)
        for t in positive_3:
            append_helper(t, mask_3, c, set_area_to_bounds, mask_3_strength)
        for t in positive_4:
            append_helper(t, mask_4, c, set_area_to_bounds, mask_4_strength)
        for t in positive_5:
            append_helper(t, mask_5, c, set_area_to_bounds, mask_5_strength)
        for t in negative_1:
            append_helper(t, mask_1, c2, set_area_to_bounds, mask_1_strength)
        for t in negative_2:
            append_helper(t, mask_2, c2, set_area_to_bounds, mask_2_strength)
        for t in negative_3:
            append_helper(t, mask_3, c2, set_area_to_bounds, mask_3_strength)
        for t in negative_4:
            append_helper(t, mask_4, c2, set_area_to_bounds, mask_4_strength)
        for t in negative_5:
            append_helper(t, mask_5, c2, set_area_to_bounds, mask_5_strength)
        return (c, c2)
    
class VRAM_Debug:
    
    @classmethod
    
    def INPUT_TYPES(s):
      return {
        "required": {
			  "model": ("MODEL",),
              "empty_cuda_cache": ("BOOLEAN", {"default": False}),
		  },
        "optional": {
            "clip_vision": ("CLIP_VISION", ),
        }
	  }
    RETURN_TYPES = ("MODEL", "INT", "INT",)
    RETURN_NAMES = ("model", "freemem_before", "freemem_after")
    FUNCTION = "VRAMdebug"
    CATEGORY = "KJNodes"

    def VRAMdebug(self, model, empty_cuda_cache, clip_vision=None):
        freemem_before = comfy.model_management.get_free_memory()
        print(freemem_before)
        if empty_cuda_cache:
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
        if clip_vision is not None:
            print("unloading clip_vision_clone")
            comfy.model_management.unload_model_clones(clip_vision.patcher)
        freemem_after = comfy.model_management.get_free_memory()
        print(freemem_after)
        return (model, freemem_before, freemem_after)
    
class SomethingToString:
    @classmethod
    
    def INPUT_TYPES(s):
     return {
        "required": {
        "input": ("*", {"forceinput": True, "default": ""}),
    },
    }
    RETURN_TYPES = ("STRING",)
    FUNCTION = "stringify"
    CATEGORY = "KJNodes"

    def stringify(self, input):
        if isinstance(input, (int, float, bool)):   
            stringified = str(input)
            print(stringified)
        else:
            return
        return (stringified,)

from nodes import EmptyLatentImage

class EmptyLatentImagePresets:
    @classmethod
    def INPUT_TYPES(cls):  
        return {
        "required": {
            "dimensions": (
            [   '512 x 512',
                '768 x 512',
                '960 x 512',
                '1024 x 512',
                '1536 x 640',
                '1536 x 640',
                '1344 x 768',
                '1216 x 832',
                '1152 x 896',
                '1024 x 1024',
            ],
            {
            "default": '512 x 512'
             }),
           
            "invert": ("BOOLEAN", {"default": False}),
            "batch_size": ("INT", {
            "default": 1,
            "min": 1,
            "max": 4096
            }),
        },
        }

    RETURN_TYPES = ("LATENT", "INT", "INT")
    RETURN_NAMES = ("Latent", "Width", "Height")
    FUNCTION = "generate"
    CATEGORY = "KJNodes"

    def generate(self, dimensions, invert, batch_size):
        result = [x.strip() for x in dimensions.split('x')]
        
        if invert:
            width = int(result[1].split(' ')[0])
            height = int(result[0])
        else:
            width = int(result[0])
            height = int(result[1].split(' ')[0])
        latent = EmptyLatentImage().generate(width, height, batch_size)[0]

        return (latent, int(width), int(height),)

#https://github.com/hahnec/color-matcher/
from color_matcher import ColorMatcher
#from color_matcher.normalizer import Normalizer

class ColorMatch:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_ref": ("IMAGE",),
                "image_target": ("IMAGE",),
                "method": (
            [   
                'mkl',
                'hm', 
                'reinhard', 
                'mvgd', 
                'hm-mvgd-hm', 
                'hm-mkl-hm',
            ], {
               "default": 'mkl'
            }),
                
            },
        }
    
    CATEGORY = "KJNodes/masking"

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "colormatch"
    
    def colormatch(self, image_ref, image_target, method):
        cm = ColorMatcher()
        batch_size = image_target.size(0)
        out = []
        images_target = image_target.squeeze()
        images_ref = image_ref.squeeze()

        image_ref_np = images_ref.numpy()
        images_target_np = images_target.numpy()

        if image_ref.size(0) > 1 and image_ref.size(0) != batch_size:
            raise ValueError("ColorMatch: Use either single reference image or a matching batch of reference images.")

        for i in range(batch_size):
            image_target_np = images_target_np if batch_size == 1 else images_target[i].numpy()
            image_ref_np_i = image_ref_np if image_ref.size(0) == 1 else images_ref[i].numpy()
            try:
                image_result = cm.transfer(src=image_target_np, ref=image_ref_np_i, method=method)
            except BaseException as e:
                print(f"Error occurred during transfer: {e}")
                break
            out.append(torch.from_numpy(image_result))
        return (torch.stack(out, dim=0).to(torch.float32), )
    
class SaveImageWithAlpha:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"
        self.prefix_append = ""

    @classmethod
    def INPUT_TYPES(s):
        return {"required": 
                    {"images": ("IMAGE", ),
                    "mask": ("MASK", ),
                    "filename_prefix": ("STRING", {"default": "ComfyUI"})},
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ()
    FUNCTION = "save_images_alpha"

    OUTPUT_NODE = True

    CATEGORY = "image"

    def save_images_alpha(self, images, mask, filename_prefix="ComfyUI_image_with_alpha", prompt=None, extra_pnginfo=None):
        filename_prefix += self.prefix_append
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0])
        results = list()

        def file_counter():
            max_counter = 0
            # Loop through the existing files
            for existing_file in os.listdir(full_output_folder):
                # Check if the file matches the expected format
                match = re.fullmatch(f"{filename}_(\d+)_?\.[a-zA-Z0-9]+", existing_file)
                if match:
                    # Extract the numeric portion of the filename
                    file_counter = int(match.group(1))
                    # Update the maximum counter value if necessary
                    if file_counter > max_counter:
                        max_counter = file_counter
            return max_counter

        for image, alpha in zip(images, mask):
            i = 255. * image.cpu().numpy()
            a = 255. * alpha.cpu().numpy()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            
            if a.shape == img.size[::-1]:  # Check if the mask has the same size as the image
                print("Applying mask")
                a = np.clip(a, 0, 255).astype(np.uint8)
                img.putalpha(Image.fromarray(a, mode='L'))
            else:
                raise ValueError("SaveImageWithAlpha: Mask size does not match")
            metadata = None
            if not args.disable_metadata:
                metadata = PngInfo()
                if prompt is not None:
                    metadata.add_text("prompt", json.dumps(prompt))
                if extra_pnginfo is not None:
                    for x in extra_pnginfo:
                        metadata.add_text(x, json.dumps(extra_pnginfo[x]))
           
            # Increment the counter by 1 to get the next available value
            counter = file_counter() + 1
            file = f"{filename}_{counter:05}.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=4)
            results.append({
                "filename": file,
                "subfolder": subfolder,
                "type": self.type
            })

        return { "ui": { "images": results } }

class ImageConcanate:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image1": ("IMAGE",),
            "image2": ("IMAGE",),
            "direction": (
            [   'right',
                'down',
                'left',
                'up',
            ],
            {
            "default": 'right'
             }),
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "concanate"
    CATEGORY = "KJNodes"

    def concanate(self, image1, image2, direction):
        if direction == 'right':
            row = torch.cat((image1, image2), dim=2)
        elif direction == 'down':
            row = torch.cat((image1, image2), dim=1)
        elif direction == 'left':
            row = torch.cat((image2, image1), dim=2)
        elif direction == 'up':
            row = torch.cat((image2, image1), dim=1)
        return (row,)
    
class ImageGridComposite2x2:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image1": ("IMAGE",),
            "image2": ("IMAGE",),
            "image3": ("IMAGE",),
            "image4": ("IMAGE",),   
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "compositegrid"
    CATEGORY = "KJNodes"

    def compositegrid(self, image1, image2, image3, image4):
        top_row = torch.cat((image1, image2), dim=2)
        bottom_row = torch.cat((image3, image4), dim=2)
        grid = torch.cat((top_row, bottom_row), dim=1)
        return (grid,)
    
class ImageGridComposite3x3:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image1": ("IMAGE",),
            "image2": ("IMAGE",),
            "image3": ("IMAGE",),
            "image4": ("IMAGE",),
            "image5": ("IMAGE",),
            "image6": ("IMAGE",),
            "image7": ("IMAGE",),
            "image8": ("IMAGE",),
            "image9": ("IMAGE",),     
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "compositegrid"
    CATEGORY = "KJNodes"

    def compositegrid(self, image1, image2, image3, image4, image5, image6, image7, image8, image9):
        top_row = torch.cat((image1, image2, image3), dim=2)
        mid_row = torch.cat((image4, image5, image6), dim=2)
        bottom_row = torch.cat((image7, image8, image9), dim=2)
        grid = torch.cat((top_row, mid_row, bottom_row), dim=1)
        return (grid,)
    
import random
class ImageBatchTestPattern:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "batch_size": ("INT", {"default": 1,"min": 1, "max": 255, "step": 1}),
            "start_from": ("INT", {"default": 1,"min": 1, "max": 255, "step": 1}),
            "width": ("INT", {"default": 512,"min": 16, "max": 4096, "step": 1}),
            "height": ("INT", {"default": 512,"min": 16, "max": 4096, "step": 1}),  
        }}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "generatetestpattern"
    CATEGORY = "KJNodes"

    def generatetestpattern(self, batch_size, start_from, width, height):
        out = []
        # Generate the sequential numbers for each image
        numbers = np.arange(batch_size) 

        # Create an image for each number
        for i, number in enumerate(numbers):
            # Create a black image with the number as a random color text
            image = Image.new("RGB", (width, height), color=0)
            draw = ImageDraw.Draw(image)

            # Draw a border around the image
            border_width = 10
            border_color = (255, 255, 255)  # white color
            border_box = [(border_width, border_width), (width - border_width, height - border_width)]
            draw.rectangle(border_box, fill=None, outline=border_color)
            
            font_size = 255  # Choose the desired font size
            font_path = "fonts\\TTNorms-Black.otf" #I don't know why relative path won't work otherwise...
            font_path = os.path.join(script_dir, font_path)
            
            # Generate a random color for the text
            color = (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            
            font = ImageFont.truetype(font_path, font_size)  # Replace "path_to_font_file.ttf" with the path to your font file
            text_width = font_size
            text_height = font_size
            text_x = (width - text_width / 2) // 2
            text_y = (height - text_height) // 2

            draw.text((text_x, text_y), str(number + start_from), font=font, fill=color)

            # Convert the image to a numpy array and normalize the pixel values
            image = np.array(image).astype(np.float32) / 255.0
            image = torch.from_numpy(image)[None,]
            out.append(image)            

        return (torch.cat(out, dim=0),)

#based on nodes from mtb https://github.com/melMass/comfy_mtb

from .utility import tensor2pil, pil2tensor, tensor2np, np2tensor

class BatchCropFromMask:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "original_images": ("IMAGE",),
                "masks": ("MASK",),
                "crop_size_mult": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
            },
        }

    RETURN_TYPES = (
        "IMAGE",
        "IMAGE",
        "BBOX",
        "INT",
        "INT",
    )
    RETURN_NAMES = (
        "original_images",
        "cropped_images",
        "bboxes",
        "width",
        "height",
    )
    FUNCTION = "crop"
    CATEGORY = "KJNodes/masking"

    def crop(self, masks, original_images, crop_size_mult):
        bounding_boxes = []
        cropped_images = []

        # First, calculate the maximum bounding box size across all masks
        max_bbox_size = 0
        for mask in masks:
            _mask = tensor2pil(mask)[0]
            non_zero_indices = np.nonzero(np.array(_mask))
            min_x, max_x = np.min(non_zero_indices[1]), np.max(non_zero_indices[1])
            min_y, max_y = np.min(non_zero_indices[0]), np.max(non_zero_indices[0])
            width = max_x - min_x
            height = max_y - min_y
            bbox_size = max(width, height)
            max_bbox_size = max(max_bbox_size, bbox_size)

        # Make sure max_bbox_size is divisible by 32, if not, round it upwards so it is
        max_bbox_size = math.ceil(max_bbox_size / 32) * 32

        # Apply the crop size multiplier
        max_bbox_size = int(max_bbox_size * crop_size_mult)

        # Then, for each mask and corresponding image...
        for mask, img in zip(masks, original_images):
            _mask = tensor2pil(mask)[0]
            non_zero_indices = np.nonzero(np.array(_mask))
            min_x, max_x = np.min(non_zero_indices[1]), np.max(non_zero_indices[1])
            min_y, max_y = np.min(non_zero_indices[0]), np.max(non_zero_indices[0])

            # Calculate center of bounding box
            center_x = (max_x + min_x) // 2
            center_y = (max_y + min_y) // 2

            # Create bounding box using max_bbox_size
            half_box_size = max_bbox_size // 2
            min_x = max(0, center_x - half_box_size)
            max_x = min(img.shape[1], center_x + half_box_size)
            min_y = max(0, center_y - half_box_size)
            max_y = min(img.shape[0], center_y + half_box_size)

            # Append bounding box coordinates
            bounding_boxes.append((min_x, min_y, max_x - min_x, max_y - min_y))

            # Crop the image from the bounding box
            cropped_img = img[min_y:max_y, min_x:max_x, :]
            cropped_images.append(cropped_img)

        cropped_out = torch.stack(cropped_images, dim=0)

        return (original_images, cropped_out, bounding_boxes, max_bbox_size, max_bbox_size, )


def bbox_to_region(bbox, target_size=None):
    bbox = bbox_check(bbox, target_size)
    return (bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3])

def bbox_check(bbox, target_size=None):
    if not target_size:
        return bbox

    new_bbox = (
        bbox[0],
        bbox[1],
        min(target_size[0] - bbox[0], bbox[2]),
        min(target_size[1] - bbox[1], bbox[3]),
    )
    return new_bbox

class BatchUncrop:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "original_images": ("IMAGE",),
                "cropped_images": ("IMAGE",),
                "bboxes": ("BBOX",),
                "border_blending": (
                    "FLOAT",
                    {"default": 0.25, "min": 0.0, "max": 1.0, "step": 0.01},
                ),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "uncrop"

    CATEGORY = "KJNodes/masking"

    def uncrop(self, original_images, cropped_images, bboxes, border_blending):
        def inset_border(image, border_width=20, border_color=(0)):
            width, height = image.size
            bordered_image = Image.new(image.mode, (width, height), border_color)
            bordered_image.paste(image, (0, 0))
            draw = ImageDraw.Draw(bordered_image)
            draw.rectangle(
                (0, 0, width - 1, height - 1), outline=border_color, width=border_width
            )
            return bordered_image

        if len(original_images) != len(cropped_images) or len(original_images) != len(bboxes):
            raise ValueError("The number of images, crop_images, and bboxes should be the same")

        input_images = tensor2pil(original_images)
        crop_imgs = tensor2pil(cropped_images)
        out_images = []
        for i in range(len(input_images)):
            img = input_images[i]
            crop = crop_imgs[i]
            bbox = bboxes[i]
            
            # uncrop the image based on the bounding box
            bb_x, bb_y, bb_width, bb_height = bbox

            paste_region = bbox_to_region((bb_x, bb_y, bb_width, bb_height), img.size)

            crop_img = crop.convert("RGB")

            if border_blending > 1.0:
                border_blending = 1.0
            elif border_blending < 0.0:
                border_blending = 0.0

            blend_ratio = (max(crop_img.size) / 2) * float(border_blending)

            blend = img.convert("RGBA")
            mask = Image.new("L", img.size, 0)

            mask_block = Image.new("L", (bb_width, bb_height), 255)
            mask_block = inset_border(mask_block, int(blend_ratio / 2), (0))

            mask.paste(mask_block, paste_region)
            blend.paste(crop_img, paste_region)

            mask = mask.filter(ImageFilter.BoxBlur(radius=blend_ratio / 4))
            mask = mask.filter(ImageFilter.GaussianBlur(radius=blend_ratio / 4))

            blend.putalpha(mask)
            img = Image.alpha_composite(img.convert("RGBA"), blend)
            out_images.append(img.convert("RGB"))

        return (pil2tensor(out_images),)


NODE_CLASS_MAPPINGS = {
    "INTConstant": INTConstant,
    "FloatConstant": FloatConstant,
    "ConditioningMultiCombine": ConditioningMultiCombine,
    "ConditioningSetMaskAndCombine": ConditioningSetMaskAndCombine,
    "ConditioningSetMaskAndCombine3": ConditioningSetMaskAndCombine3,
    "ConditioningSetMaskAndCombine4": ConditioningSetMaskAndCombine4,
    "ConditioningSetMaskAndCombine5": ConditioningSetMaskAndCombine5,
    "GrowMaskWithBlur": GrowMaskWithBlur,
    "ColorToMask": ColorToMask,
    "CreateGradientMask": CreateGradientMask,
    "CreateTextMask": CreateTextMask,
    "CreateAudioMask": CreateAudioMask,
    "CreateFadeMask": CreateFadeMask,
    "CreateFluidMask" :CreateFluidMask,
    "VRAM_Debug" : VRAM_Debug,
    "SomethingToString" : SomethingToString,
    "CrossFadeImages": CrossFadeImages,
    "EmptyLatentImagePresets": EmptyLatentImagePresets,
    "ColorMatch": ColorMatch,
    "GetImageRangeFromBatch": GetImageRangeFromBatch,
    "SaveImageWithAlpha": SaveImageWithAlpha,
    "ReverseImageBatch": ReverseImageBatch,
    "ImageGridComposite2x2": ImageGridComposite2x2,
    "ImageGridComposite3x3": ImageGridComposite3x3,
    "ImageConcanate": ImageConcanate,
    "ImageBatchTestPattern": ImageBatchTestPattern,
    "ReplaceImagesInBatch": ReplaceImagesInBatch,
    "BatchCropFromMask": BatchCropFromMask,
    "BatchUncrop": BatchUncrop,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "INTConstant": "INT Constant",
    "FloatConstant": "Float Constant",
    "ConditioningMultiCombine": "Conditioning Multi Combine",
    "ConditioningSetMaskAndCombine": "ConditioningSetMaskAndCombine",
    "ConditioningSetMaskAndCombine3": "ConditioningSetMaskAndCombine3",
    "ConditioningSetMaskAndCombine4": "ConditioningSetMaskAndCombine4",
    "ConditioningSetMaskAndCombine5": "ConditioningSetMaskAndCombine5",
    "GrowMaskWithBlur": "GrowMaskWithBlur",
    "ColorToMask": "ColorToMask",
    "CreateGradientMask": "CreateGradientMask",
    "CreateTextMask" : "CreateTextMask",
    "CreateFadeMask" : "CreateFadeMask",
    "CreateFluidMask" : "CreateFluidMask",
    "VRAM_Debug" : "VRAM Debug",
    "CrossFadeImages": "CrossFadeImages",
    "SomethingToString": "SomethingToString",
    "EmptyLatentImagePresets": "EmptyLatentImagePresets",
    "ColorMatch": "ColorMatch",
    "GetImageRangeFromBatch": "GetImageRangeFromBatch",
    "SaveImageWithAlpha": "SaveImageWithAlpha",
    "ReverseImageBatch": "ReverseImageBatch",
    "ImageGridComposite2x2": "ImageGridComposite2x2",
    "ImageGridComposite3x3": "ImageGridComposite3x3",
    "ImageConcanate": "ImageConcanate",
    "ImageBatchTestPattern": "ImageBatchTestPattern",
    "ReplaceImagesInBatch": "ReplaceImagesInBatch",
    "BatchCropFromMask": "BatchCropFromMask",
    "BatchUncrop": "BatchUncrop",
}