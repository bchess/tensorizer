import argparse
import gc
import json
import logging
import os
import tempfile
import time
from typing import Optional, Type, Union

import torch
from diffusers import (
    AutoencoderKL,
    ConfigMixin,
    LMSDiscreteScheduler,
    ModelMixin,
    StableDiffusionPipeline,
    UNet2DConditionModel,
)
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    CLIPTextConfig,
    CLIPTextModel,
    PretrainedConfig,
    PreTrainedModel,
)

from tensorizer import TensorDeserializer, TensorSerializer, stream_io, utils

# Setup logger
logger = logging.getLogger(__name__)
logger.setLevel(level=logging.INFO)
fh = logging.StreamHandler()
fh_formatter = logging.Formatter(
    "%(asctime)s %(levelname)s %(filename)s(%(process)d) - %(message)s"
)
fh.setFormatter(fh_formatter)
logger.addHandler(fh)


# Now you can use these credentials to create a boto3 client


def check_file_exists(file):
    if os.path.exists(file):
        return True
    else:
        with stream_io.open_stream(file, "rb") as f:
            if f.read(1) == b'':
                return False
            else:
                return True


def serialize_model(
        model: torch.nn.Module,
        config: Optional[Union[ConfigMixin, AutoConfig, dict]],
        model_directory: str,
        model_prefix: str = None,
        force: bool = False,
):
    """
    Remove the tensors from a PyTorch model, convert them to NumPy
    arrays and serialize them to Tensorizer format. The stripped
    model is also serialized to pytorch format.

    Args:
        model: The model to serialize.
        config: The model's configuration. This is optional and only
            required for HuggingFace Transformers models. Diffusers
            models do not require this.
        model_directory: The directory to save the serialized model to.
        model_prefix: The prefix to use for the serialized model files. This
            is purely optional, and it allows for multiple models to be
            serialized to the same directory. A good example are Stable
            Diffusion models. Default is "model".
        force: Force upload serialized tensors to `output_prefix`
            even if they already exist
    """

    if model_prefix is None:
        model_prefix = "model"

    dir_prefix = f"{model_directory}/{model_prefix}"
    config_file_exists, weights_file_exists = (check_file_exists(f"{dir_prefix}-config.json"),
                                               check_file_exists(f"{dir_prefix}.tensors"))
    if config is None:
        config = model
    if config is not None:
        config_path = f"{dir_prefix}-config.json"
        if (not config_file_exists) or force:
            logger.info(f"Writing config to {config_path}")
            with stream_io.open_stream(config_path, "wb") as f:
                if hasattr(config, "to_dict"):
                    f.write(bytes(json.dumps(config.to_dict()), "utf-8"))
                elif isinstance(config, dict):
                    f.write(bytes(json.dumps(config), "utf-8"))
                f.close()  ## Remove after PR

    if (not weights_file_exists) or force:
        logger.info(f"Writing tensors to {dir_prefix}.tensors")
        ts = TensorSerializer(f"{dir_prefix}.tensors")
        ts.write_module(model)
        ts.close()


def load_model(
        path_uri: str,
        model_class: Union[
            Type[PreTrainedModel], Type[ModelMixin], Type[ConfigMixin]
        ],
        config_class: Optional[
            Union[Type[PretrainedConfig], Type[ConfigMixin], Type[AutoConfig]]
        ] = None,
        model_prefix: Optional[str] = "model",
        device: torch.device = utils.get_device(),
        dtype: Optional[str] = None,
) -> torch.nn.Module:
    """
    Given a path prefix, load the model with a custom extension

    Args:
        path_uri: path to the model. Can be a local path or a URI
        model_class: The model class to load the tensors into.
        config_class: The config class to load the model config into. This must be
            set if you are loading a model from HuggingFace Transformers.
        model_prefix: The prefix to use to distinguish between multiple serialized
            models. The default is "model".
        device: The device onto which to load the model.
        dtype: The dtype to load the tensors into. If None, the dtype is inferred from
            the model.
    """
    if model_prefix is None:
        model_prefix = "model"

    begin_load = time.time()
    ram_usage = utils.get_mem_usage()

    config_uri = f"{path_uri}/{model_prefix}-config.json"
    tensors_uri = f"{path_uri}/{model_prefix}.tensors"

    logger.info(f"Loading {tensors_uri}, {ram_usage}")
    tensor_stream = stream_io.open_stream(tensors_uri)

    tensor_deserializer = TensorDeserializer(
        tensor_stream, device=device, dtype=dtype, lazy_load=True
    )

    if config_class is not None:
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_config_path = os.path.join(temp_dir, "config.json")
                with open(temp_config_path, "wb") as temp_config:
                    logger.info(f"Loading {config_uri}, {ram_usage}")
                    temp_config.write(stream_io.open_stream(config_uri).read())
                config = config_class.from_pretrained(temp_dir)
                config.gradient_checkpointing = True
        except ValueError:
            config = config_class.from_pretrained(config_uri)
        with utils.no_init_or_tensor():
            # AutoModels instantiate from a config via their from_config()
            # method, while other classes can usually be instantiated directly.
            config_loader = getattr(model_class, "from_config", model_class)
            model = config_loader(config)
    else:
        try:
            config = json.loads(
                stream_io.open_stream(config_uri).read().decode("utf-8")
            )
        except ValueError:
            with open(config_uri, "r") as f:
                config = json.load(f)
        with utils.no_init_or_tensor():
            model = model_class(**config)

    tensor_deserializer.load_into_module(model)

    tensor_load_s = time.time() - begin_load
    rate_str = utils.convert_bytes(
        tensor_deserializer.total_bytes_read / tensor_load_s
    )
    tensors_sz = utils.convert_bytes(tensor_deserializer.total_bytes_read)
    logger.info(
        f"Model tensors loaded in {tensor_load_s:0.2f}s, read "
        f"{tensors_sz} @ {rate_str}/s, {utils.get_mem_usage()}"
    )

    return model


def df_main(args: argparse.Namespace) -> None:
    output_prefix = args.output_prefix
    print("MODEL PATH:", args.input_directory)
    print("OUTPUT PREFIX:", output_prefix)

    hf_api_token = os.environ.get("HF_API_TOKEN")

    pipeline = StableDiffusionPipeline.from_pretrained(
        args.input_directory, use_auth_token=hf_api_token
    )

    logger.info("Serializing model")
    logger.info("GPU: " + utils.get_gpu_name())
    logger.info("PYTHON USED RAM: " + utils.get_mem_usage())

    serialize_model(pipeline.text_encoder.eval(), pipeline.text_encoder.config, output_prefix, "encoder")
    serialize_model(pipeline.vae.eval(), None, output_prefix, "vae")
    serialize_model(pipeline.unet.eval(), None, output_prefix, "unet")

    pipeline.tokenizer.save_pretrained(output_prefix)
    pipeline.scheduler.save_pretrained(output_prefix)

    if args.validate:
        del pipeline
        gc.collect()
        device = utils.get_device()

        logger.info("Validating serialization")
        vae = load_model(output_prefix, AutoencoderKL, None, "vae", device)
        unet = load_model(
            output_prefix, UNet2DConditionModel, None, "unet", device
        )
        encoder = load_model(
            output_prefix, CLIPTextModel, CLIPTextConfig, "encoder", device
        )

        pipeline = StableDiffusionPipeline(
            text_encoder=encoder,
            vae=vae,
            unet=unet,
            tokenizer=AutoTokenizer.from_pretrained(
                args.input_directory, subfolder="tokenizer"
            ),
            scheduler=LMSDiscreteScheduler.from_pretrained(
                args.input_directory, subfolder="scheduler"
            ),
            safety_checker=None,
            feature_extractor=None,
            requires_safety_checker=False,
        ).to(device)

        prompt = "a photo of an astronaut riding a horse on mars"
        with torch.autocast(device.type):
            pipeline(prompt).images[0].save("test.png")


def hf_main(args):
    output_prefix = args.output_prefix

    # May not be necessary if users can be assumed won't accidentally
    # include trailing slashes in their output_prefix
    output_prefix = output_prefix[:-1] if output_prefix[-1] == "/" else output_prefix

    print("MODEL PATH:", args.input_directory)
    print("OUTPUT PREFIX:", output_prefix)

    dtype = torch.float16
    model_config = AutoConfig.from_pretrained(args.input_directory)
    model = AutoModelForCausalLM.from_pretrained(
        args.input_directory,
        config=model_config,
        torch_dtype=dtype,
        low_cpu_mem_usage=True,
    )

    logger.info("Serializing model")
    logger.info("GPU: " + utils.get_gpu_name())
    logger.info("PYTHON USED RAM: " + utils.get_mem_usage())

    serialize_model(model,
                    model_config,
                    output_prefix,
                    None,
                    args.force)

    if args.validate:
        # Not sure if this part is needed as, although I doubt it,
        # I'm not absolutely certain whether or not TensorSerializer
        # changes the model's state in any way
        del model
        gc.collect()
        device = utils.get_device()
        model = AutoModelForCausalLM.from_pretrained(
            args.input_directory,
            config=model_config,
            torch_dtype=dtype,
            low_cpu_mem_usage=True,
        )
        model.to(device)
        logger.info("Validating serialization")
        model2 = load_model(
            output_prefix,
            AutoModelForCausalLM,
            AutoConfig,
            None,
            device,
            dtype,
        ).eval()
        # Comparing model parameters
        logger.info("Testing for sameness of model parameters")
        for name, param in model.named_parameters():
            param2 = model2.state_dict()[name]
            assert torch.allclose(param, param2, atol=1e-3)


def main():
    # usage: hf_serialization.py [-h] --model_type {transformers,diffusers} [--validate] input_directory output_prefix

    parser = argparse.ArgumentParser(
        description=(
            "An example script that uses Tensorizer to serialize"
            "a HuggingFace model to an output directory."
        )
    )
    parser.add_argument(
        "input_directory",
        type=str,
        help="Path to model directory or HF model ID",
    )
    parser.add_argument(
        "output_prefix", type=str, help="Path to output directory"
    )
    parser.add_argument(
        "--model_type",
        type=str,
        choices=["transformers", "diffusers"],
        required=True,
        help="Framework used for the model",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate serialization by running a test inference",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force upload serialized tensors to output_prefix even if they already exist",
    )
    args = parser.parse_args()

    if args.model_type == "transformers":
        hf_main(args)
    elif args.model_type == "diffusers":
        df_main(args)
    else:
        raise ValueError(
            f"Unknown model type {args.model_type} (transformers or diffusers)"
        )


if __name__ == "__main__":
    main()
