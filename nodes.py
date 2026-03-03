import os
import torch
import torchaudio
import folder_paths
import logging

try:
    from LavaSR.model import LavaEnhance2, LavaEnhance
    from LavaSR.enhancer.linkwitz_merge import FastLRMerge
    LAVASR_AVAILABLE = True
except ImportError:
    logging.warning("LavaSR is not installed. Please run: pip install git+https://github.com/ysharma3501/LavaSR.git")
    LAVASR_AVAILABLE = False


# Register the custom model path
lavasr_models_dir = os.path.join(folder_paths.models_dir, "lavasr")
try:
    if not os.path.exists(lavasr_models_dir):
        os.makedirs(lavasr_models_dir, exist_ok=True)
except Exception:
    pass

folder_paths.folder_names_and_paths["lavasr"] = ([lavasr_models_dir], set())

# Global cache to avoid excessive RAM/VRAM reloading
_LAVASR_CACHED_MODEL_ID = None
_LAVASR_CACHED_MODEL = None


class LavaSREnhanceNode:
    @classmethod
    def INPUT_TYPES(s):
        paths = folder_paths.get_folder_paths("lavasr")
        local_models = []
        for p in paths:
            if os.path.exists(p):
                for dir_entry in os.listdir(p):
                    if os.path.isdir(os.path.join(p, dir_entry)):
                        local_models.append(dir_entry)
                        
        # Provide default HuggingFace Hub IDs
        default_models = ["YatharthS/LavaSR"]
        available_models = default_models + [m for m in local_models if m not in default_models]

        return {
            "required": {
                "audio": ("AUDIO",),
                "model_id": (available_models,),
                "version": (["LavaEnhance2", "LavaEnhance1"],),
                "sampling_rate": ("INT", {"default": 16000, "min": 8000, "max": 48000, "step": 1000, "display": "number", "tooltip": "Match this to your source audio's quality."}),
                "denoise": ("BOOLEAN", {"default": False, "tooltip": "Change this to True only if your audio has noise you want to filter."}),
                "batch_processing": ("BOOLEAN", {"default": False, "tooltip": "Change this to True if audio is very long."}),
            }
        }

    RETURN_TYPES = ("AUDIO",)
    FUNCTION = "enhance_audio"
    CATEGORY = "audio/enhancement"

    def enhance_audio(self, audio, model_id, version, sampling_rate, denoise, batch_processing):
        global _LAVASR_CACHED_MODEL_ID
        global _LAVASR_CACHED_MODEL

        if not LAVASR_AVAILABLE:
            raise RuntimeError("LavaSR is not installed. Please install it with: pip install git+https://github.com/ysharma3501/LavaSR.git")

        device = "cuda" if torch.cuda.is_available() else "cpu"

        # Resolve model path. If it's a local folder matching the name, use that full path.
        loaded_model_path = model_id
        for p in folder_paths.get_folder_paths("lavasr"):
            full_path = os.path.join(p, model_id)
            if os.path.isdir(full_path):
                loaded_model_path = full_path
                break
        
        # If it's a HuggingFace hub string and not already an absolute path to a folder
        if not os.path.exists(loaded_model_path) and "/" in loaded_model_path:
            try:
                from huggingface_hub import snapshot_download
                local_dir = os.path.join(lavasr_models_dir, loaded_model_path.replace("/", "_"))
                logging.info(f"[LavaSR] Downloading {loaded_model_path} to {local_dir}")
                loaded_model_path = snapshot_download(
                    repo_id=loaded_model_path,
                    local_dir=local_dir,
                    local_dir_use_symlinks=False
                )
            except ImportError:
                logging.warning("huggingface_hub is not installed. Will attempt to let LavaSR download to default cache.")

        cache_key = f"{loaded_model_path}_{version}"

        if _LAVASR_CACHED_MODEL is None or _LAVASR_CACHED_MODEL_ID != cache_key:
            logging.info(f"[LavaSR] Loading model {loaded_model_path} ({version}) onto {device}...")
            if version == "LavaEnhance2":
                _LAVASR_CACHED_MODEL = LavaEnhance2(loaded_model_path, device)
            else:
                _LAVASR_CACHED_MODEL = LavaEnhance(loaded_model_path, device)
            
            _LAVASR_CACHED_MODEL_ID = cache_key
            logging.info(f"[LavaSR] Model loaded successfully.")

        model = _LAVASR_CACHED_MODEL

        waveform = audio.get("waveform")
        source_sr = audio.get("sample_rate", audio.get("sample_rate"))
        
        if waveform is None:
            raise ValueError("Input audio does not contain a waveform tensor.")

        # Replicate LavaSR `load_audio` lr_refiner configuration mechanics
        cutoff = sampling_rate // 2
        model.bwe_model.lr_refiner = FastLRMerge(device=device, cutoff=cutoff, transition_bins=1024)

        out_waveforms = []
        
        # waveform structure in ComfyUI: [batch, channels, samples]
        # We process each batch, each channel separately because LavaSR returns 1D arrays for enhance()
        for b in range(waveform.shape[0]):
            audio_batch = waveform[b] # [channels, samples]
            
            processed_channels = []
            for ch in range(audio_batch.shape[0]):
                in_audio = audio_batch[ch].to(device)

                # LavaSR enhance() natively expects the input tensor *to be exactly 16000Hz* physically.
                # If the current audio is not 16000Hz, resample it.
                if source_sr != 16000:
                    in_audio = torchaudio.functional.resample(in_audio, source_sr, 16000)
                
                # The denoiser asserts `input.ndim == 2  # mono input` internally.
                # It expects [1, samples] for a mono track.
                # Since we pulled out a single channel, it's [samples]. We just unsqueeze it to [1, samples]!
                if in_audio.dim() == 1:
                    in_audio = in_audio.unsqueeze(0)

                with torch.inference_mode():
                    # enhance() internally outputs a 1D tensor [samples] at 48000Hz
                    try:
                        out_audio = model.enhance(in_audio, denoise=denoise, batch=batch_processing).cpu()
                    except RuntimeError as e:
                        # Fallback for CPU OOM or similar if too large
                        logging.warning(f"LavaSR enhance failed: {e}. Trying to force batch_processing=True.")
                        out_audio = model.enhance(in_audio, denoise=denoise, batch=True).cpu()
                        
                processed_channels.append(out_audio)
                
            out_channels = torch.stack(processed_channels, dim=0)
            out_waveforms.append(out_channels)

        final_waveform = torch.stack(out_waveforms, dim=0)

        # LavaSR always returns 48000Hz audio
        return ({"waveform": final_waveform, "sample_rate": 48000}, )

NODE_CLASS_MAPPINGS = {
    "LavaSREnhanceNode": LavaSREnhanceNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LavaSREnhanceNode": "LavaSR Audio Enhance"
}
