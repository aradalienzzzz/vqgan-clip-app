import streamlit as st
import argparse
import math
from pathlib import Path

import sys

sys.path.append("./taming-transformers")

from PIL import Image
import requests
import torch
from torch import nn, optim
from torch.nn import functional as F
from torchvision import transforms
from torchvision.transforms import functional as TF
from tqdm import tqdm
from CLIP import clip

from utils import (
    load_vqgan_model,
    MakeCutouts,
    parse_prompt,
    resize_image,
    Prompt,
    fetch,
    synth,
    checkin,
)
from typing import Optional
from omegaconf import OmegaConf
import imageio
import numpy as np

# Hacky method to preserve state
# class State:
#     model = None
#     perceptor = None
#     prev_im = None
#
# state = State()

# Preserve state in Streamlit
# st.session_state["model"] = None
# st.session_state["perceptor"] = None
# st.session_state["prev_im"] = None

# Load defaults
# By default, `defaults.yaml` contains the following:
# weights: 0
# num_steps: 100
# Xdim: 375
# ydim: 240
# set_seed: false
# seed: 0
# continue_previous_run: false
defaults = OmegaConf.load("defaults.yaml")


def run(
    # Inputs
    text_input: str = "the first day of the waters",
    vqgan_ckpt: str = "vqgan_imagenet_f16_16384",
    num_steps: int = 300,
    image_x: int = 300,
    image_y: int = 300,
    continue_prev_run: bool = False,
    seed: Optional[int] = None,
    **kwargs,  # Use this to receive Streamlit objects
):
    # Leaving most of this untouched
    args = argparse.Namespace(
        prompts=[text_input],
        image_prompts=[],
        noise_prompt_seeds=[],
        noise_prompt_weights=[],
        size=[int(image_x), int(image_y)],
        # init_image=None,
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
    print("Using device:", device)

    if continue_prev_run is True:
        # Streamlit tie-in -----------------------------------
        model = st.session_state["model"]
        perceptor = st.session_state["perceptor"]
        # End of Streamlit tie-in ----------------------------

    else:
        # Streamlit tie-in -----------------------------------
        # Remove the cache first! CUDA out of memory
        if "model" in st.session_state:
            del st.session_state["model"]

        if "perceptor" in st.session_state:
            del st.session_state["perceptor"]

        # debug_slot.write(st.session_state) # DEBUG

        model = st.session_state["model"] = load_vqgan_model(
            args.vqgan_config, args.vqgan_checkpoint
        ).to(device)
        perceptor = st.session_state["perceptor"] = (
            clip.load(args.clip_model, jit=False)[0]
            .eval()
            .requires_grad_(False)
            .to(device)
        )
        # End of Streamlit tie-in ----------------------------

    cut_size = perceptor.visual.input_resolution
    e_dim = model.quantize.e_dim
    f = 2 ** (model.decoder.num_resolutions - 1)
    make_cutouts = MakeCutouts(cut_size, args.cutn, cut_pow=args.cut_pow)
    n_toks = model.quantize.n_e
    toksX, toksY = args.size[0] // f, args.size[1] // f
    sideX, sideY = toksX * f, toksY * f
    z_min = model.quantize.embedding.weight.min(dim=0).values[None, :, None, None]
    z_max = model.quantize.embedding.weight.max(dim=0).values[None, :, None, None]

    if args.seed is not None:
        torch.manual_seed(args.seed)

    # if args.init_image:
    #     pil_image = Image.open(fetch(args.init_image)).convert('RGB')
    if continue_prev_run:
        # Streamlit tie-in -----------------------------------
        pil_image = st.session_state["prev_im"]
        # End of Streamlit tie-in ----------------------------

        pil_image = pil_image.resize((sideX, sideY), Image.LANCZOS)
        z, *_ = model.encode(TF.to_tensor(pil_image).to(device).unsqueeze(0) * 2 - 1)
    else:
        one_hot = F.one_hot(
            torch.randint(n_toks, [toksY * toksX], device=device), n_toks
        ).float()
        z = one_hot @ model.quantize.embedding.weight
        z = z.view([-1, toksY, toksX, e_dim]).permute(0, 3, 1, 2)
    z_orig = z.clone()
    z.requires_grad_(True)
    opt = optim.Adam([z], lr=args.step_size)

    normalize = transforms.Normalize(
        mean=[0.48145466, 0.4578275, 0.40821073],
        std=[0.26862954, 0.26130258, 0.27577711],
    )

    pMs = []

    for prompt in args.prompts:
        txt, weight, stop = parse_prompt(prompt)
        embed = perceptor.encode_text(clip.tokenize(txt).to(device)).float()
        pMs.append(Prompt(embed, weight, stop).to(device))

    for prompt in args.image_prompts:
        path, weight, stop = parse_prompt(prompt)
        img = resize_image(Image.open(fetch(path)).convert("RGB"), (sideX, sideY))
        batch = make_cutouts(TF.to_tensor(img).unsqueeze(0).to(device))
        embed = perceptor.encode_image(normalize(batch)).float()
        pMs.append(Prompt(embed, weight, stop).to(device))

    for seed, weight in zip(args.noise_prompt_seeds, args.noise_prompt_weights):
        gen = torch.Generator().manual_seed(seed)
        embed = torch.empty([1, perceptor.visual.output_dim]).normal_(generator=gen)
        pMs.append(Prompt(embed, weight).to(device))

    def ascend_txt():
        out = synth(model, z)
        iii = perceptor.encode_image(normalize(make_cutouts(out))).float()

        result = []

        if args.init_weight:
            result.append(F.mse_loss(z, z_orig) * args.init_weight / 2)

        for prompt in pMs:
            result.append(prompt(iii))

        return result

    # Streamlit tie-in -----------------------------------

    status_text.text("Running ...")
    step_counter = 0
    frames = []

    # While loop to accomodate running predetermined steps or running indefinitely
    while True:
        opt.zero_grad()
        lossAll = ascend_txt()
        im = checkin(step_counter, lossAll, model, z)

        step_progress_bar.progress((step_counter + 1) / num_steps)
        im_display_slot.image(im, caption="Output image")

        # Save image at every step
        st.session_state["prev_im"] = im

        # ref: https://stackoverflow.com/a/33117447/13095028
        # im_byte_arr = io.BytesIO()
        # im.save(im_byte_arr, format="JPEG")
        # frames.append(im_byte_arr.getvalue()) # read()
        frames.append(np.asarray(im))

        # End of Stremalit tie-in ------------------------

        loss = sum(lossAll)
        loss.backward()
        opt.step()
        with torch.no_grad():
            z.copy_(z.maximum(z_min).minimum(z_max))

        step_counter += 1
        if (step_counter == num_steps) and num_steps > 0:
            break

    # Stitch into video using imageio
    writer = imageio.get_writer("temp.mp4", fps=24)
    for frame in frames:
        writer.append_data(frame)
    writer.close()

    status_text.text("Done!")

    # End of Stremalit tie-in ----------------------------

    return im


if __name__ == "__main__":
    st.set_page_config(page_title="VQGAN-CLIP playground")
    st.title("VQGAN-CLIP playground")

    with st.form("form-inputs"):
        # Only element not in the sidebar, but in the form
        text_input = st.text_input(
            "Prompt text",
            help="VQGAN-CLIP will generate an image that best fits the prompt",
        )
        radio = st.sidebar.radio(
            "Model weights",
            [
                "vqgan_imagenet_f16_1024",
                "vqgan_imagenet_f16_16384",
                "coco",
                "faceshq",
                "sflickr",
                "wikiart_16384",
                "wikiart_1024",
            ],
            index=defaults["weights"],
            help="Choose which weights to load, trained on different datasets",
        )
        num_steps = st.sidebar.number_input(
            "Num steps",
            value=defaults["num_steps"],
            min_value=-1,
            max_value=None,
            step=1,
            help="Specify -1 to run indefinitely. Use Streamlit's stop button in the top right corner to terminate execution",
        )

        image_x = st.sidebar.number_input(
            "Xdim", value=defaults["Xdim"], help="Width of output image, in pixels"
        )
        image_y = st.sidebar.number_input(
            "ydim", value=defaults["ydim"], help="Height of output image, in pixels"
        )
        set_seed = st.sidebar.checkbox(
            "Set seed",
            value=defaults["set_seed"],
            help="Check to set random seed for reproducibility. Will add option to specify seed",
        )

        seed_widget = st.sidebar.empty()
        if set_seed is True:
            seed = seed_widget.number_input(
                "seed", value=defaults["seed"], help="Random seed to use"
            )
        else:
            seed = None
        continue_prev_run = st.sidebar.checkbox(
            "Continue previous run",
            value=defaults["continue_prev_run"],
            help="Use existing image and existing weights for the next run",
        )
        submitted = st.form_submit_button("Run!")
        status_text = st.empty()
        status_text.text("Pending input prompt")
        step_progress_bar = st.progress(0)

    im_display_slot = st.empty()
    vid_display_slot = st.empty()
    debug_slot = st.empty()

    if "prev_im" in st.session_state:
        im_display_slot.image(st.session_state["prev_im"])

    with st.beta_expander("Expand for README"):
        with open("README.md", "r") as f:
            description = f.read()
        st.write(description)

    if submitted:
        # debug_slot.write(st.session_state) # DEBUG
        status_text.text("Loading weights ...")
        im = run(
            # Inputs
            text_input=text_input,
            vqgan_ckpt=radio,
            num_steps=num_steps,
            image_x=int(image_x),
            image_y=int(image_y),
            seed=int(seed) if set_seed is True else None,
            continue_prev_run=continue_prev_run,
            im_display_slot=im_display_slot,
            step_progress_bar=step_progress_bar,
            status_text=status_text,
        )
        vid_display_slot.video("temp.mp4")
        # debug_slot.write(st.session_state) # DEBUG
