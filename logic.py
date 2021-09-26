from typing import Optional, List, Tuple
from PIL import Image
import argparse
import clip
from utils import (
    load_vqgan_model,
    MakeCutouts,
    parse_prompt,
    resize_image,
    Prompt,
    synth,
    checkin,
)
import torch
from torchvision.transforms import functional as TF
import torch.nn as nn
from torch.nn import functional as F
from torch import optim
from torchvision import transforms


class Run:
    """
    Subclass this to house your own implementation of CLIP-based image generation 
    models within the UI
    """

    def __init__(self):
        """
        Set up the run's config here
        """
        pass

    def load_model(self):
        """
        Load models here. Separated this from __init__ to allow loading model state
        from a previous run
        """
        pass

    def model_init(self):
        """
        Continue run setup, for items that require the models to be in=place. 
        Call once after load_model
        """
        pass

    def iterate(self):
        """
        Place iteration logic here. Outputs results for human consumption at
        every step. 
        """
        pass


class VQGANCLIPRun(Run):
    def __init__(
        # Inputs
        self,
        text_input: str = "the first day of the waters",
        vqgan_ckpt: str = "vqgan_imagenet_f16_16384",
        num_steps: int = 300,
        image_x: int = 300,
        image_y: int = 300,
        init_image: Optional[Image.Image] = None,
        image_prompts: List[Image.Image] = [],
        continue_prev_run: bool = False,
        seed: Optional[int] = None,
        ## **kwargs,  # Use this to receive Streamlit objects ## Call from main UI
    ) -> None:
        super().__init__()
        self.text_input = text_input
        self.vqgan_ckpt = vqgan_ckpt
        self.num_steps = num_steps
        self.image_x = image_x
        self.image_y = image_y
        self.init_image = init_image
        self.image_prompts = image_prompts
        self.continue_prev_run = continue_prev_run
        self.seed = seed

        # Setup ------------------------------------------------------------------------------
        # Split text by "|" symbol
        texts = [phrase.strip() for phrase in text_input.split("|")]
        if texts == [""]:
            texts = []

        # Leaving most of this untouched
        self.args = argparse.Namespace(
            prompts=texts,
            image_prompts=image_prompts,
            noise_prompt_seeds=[],
            noise_prompt_weights=[],
            size=[int(image_x), int(image_y)],
            init_image=init_image,
            init_weight=0.0,
            # clip.available_models()
            # ['RN50', 'RN101', 'RN50x4', 'ViT-B/32']
            # Visual Transformer seems to be the smallest
            clip_model="ViT-B/32",
            vqgan_config=f"assets/{vqgan_ckpt}.yaml",
            vqgan_checkpoint=f"assets/{vqgan_ckpt}.ckpt",
            step_size=0.05,
            cutn=64,
            cut_pow=1.0,
            display_freq=50,
            seed=seed,
        )

        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.device = device
        print("Using device:", device)

    def load_model(
        self, prev_model: nn.Module = None, prev_perceptor: nn.Module = None
    ) -> Optional[Tuple[nn.Module, nn.Module]]:
        if self.continue_prev_run is True:
            self.model = prev_model
            self.perceptor = prev_perceptor
            return None

        else:
            self.model = load_vqgan_model(
                self.args.vqgan_config, self.args.vqgan_checkpoint
            ).to(self.device)

            self.perceptor = (
                clip.load(self.args.clip_model, jit=False)[0]
                .eval()
                .requires_grad_(False)
                .to(self.device)
            )

            return self.model, self.perceptor

    def model_init(self, init_image: Image.Image = None) -> None:
        cut_size = self.perceptor.visual.input_resolution
        e_dim = self.model.quantize.e_dim
        f = 2 ** (self.model.decoder.num_resolutions - 1)
        self.make_cutouts = MakeCutouts(
            cut_size, self.args.cutn, cut_pow=self.args.cut_pow
        )
        n_toks = self.model.quantize.n_e
        toksX, toksY = self.args.size[0] // f, self.args.size[1] // f
        sideX, sideY = toksX * f, toksY * f
        self.z_min = self.model.quantize.embedding.weight.min(dim=0).values[
            None, :, None, None
        ]
        self.z_max = self.model.quantize.embedding.weight.max(dim=0).values[
            None, :, None, None
        ]

        if self.seed is not None:
            torch.manual_seed(self.seed)
        else:
            self.seed = torch.seed()  # Trigger a seed, retrieve the utilized seed

        # Initialization order: continue_prev_im, init_image, then only random init
        if init_image is not None:
            init_image = init_image.resize((sideX, sideY), Image.LANCZOS)
            self.z, *_ = self.model.encode(
                TF.to_tensor(init_image).to(self.device).unsqueeze(0) * 2 - 1
            )
        elif self.args.init_image:
            pil_image = self.args.init_image
            pil_image = pil_image.resize((sideX, sideY), Image.LANCZOS)
            self.z, *_ = self.model.encode(
                TF.to_tensor(pil_image).to(self.device).unsqueeze(0) * 2 - 1
            )
        else:
            one_hot = F.one_hot(
                torch.randint(n_toks, [toksY * toksX], device=self.device), n_toks
            ).float()
            self.z = one_hot @ self.model.quantize.embedding.weight
            self.z = self.z.view([-1, toksY, toksX, e_dim]).permute(0, 3, 1, 2)
        self.z_orig = self.z.clone()
        self.z.requires_grad_(True)
        self.opt = optim.Adam([self.z], lr=self.args.step_size)

        self.normalize = transforms.Normalize(
            mean=[0.48145466, 0.4578275, 0.40821073],
            std=[0.26862954, 0.26130258, 0.27577711],
        )

        self.pMs = []

        for prompt in self.args.prompts:
            txt, weight, stop = parse_prompt(prompt)
            embed = self.perceptor.encode_text(
                clip.tokenize(txt).to(self.device)
            ).float()
            self.pMs.append(Prompt(embed, weight, stop).to(self.device))

        for uploaded_image in self.args.image_prompts:
            # path, weight, stop = parse_prompt(prompt)
            # img = resize_image(Image.open(fetch(path)).convert("RGB"), (sideX, sideY))
            img = resize_image(uploaded_image.convert("RGB"), (sideX, sideY))
            batch = self.make_cutouts(TF.to_tensor(img).unsqueeze(0).to(self.device))
            embed = self.perceptor.encode_image(self.normalize(batch)).float()
            self.pMs.append(Prompt(embed, weight, stop).to(self.device))

        for seed, weight in zip(
            self.args.noise_prompt_seeds, self.args.noise_prompt_weights
        ):
            gen = torch.Generator().manual_seed(seed)
            embed = torch.empty([1, self.perceptor.visual.output_dim]).normal_(
                generator=gen
            )
            self.pMs.append(Prompt(embed, weight).to(self.device))

    def _ascend_txt(self) -> List:
        out = synth(self.model, self.z)
        iii = self.perceptor.encode_image(
            self.normalize(self.make_cutouts(out))
        ).float()

        result = []

        if self.args.init_weight:
            result.append(F.mse_loss(self.z, self.z_orig) * self.args.init_weight / 2)

        for prompt in self.pMs:
            result.append(prompt(iii))

        return result

    def iterate(self) -> Tuple[List[float], Image.Image]:
        # Forward prop
        self.opt.zero_grad()
        losses = self._ascend_txt()

        # Grab an image
        im: Image.Image = checkin(self.model, self.z)

        # Backprop
        loss = sum(losses)
        loss.backward()
        self.opt.step()
        with torch.no_grad():
            self.z.copy_(self.z.maximum(self.z_min).minimum(self.z_max))

        # Output stuff useful for humans
        return [loss.item() for loss in losses], im