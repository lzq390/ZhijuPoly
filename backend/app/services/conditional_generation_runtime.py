from __future__ import annotations

import gc
import importlib
import math
from pathlib import Path
from threading import Lock
from typing import Any

import joblib
import numpy as np
from rdkit import Chem
from rdkit.Chem import Descriptors

from app.services.conditional_generation import GeneratedSmiles, to_model_smiles, to_rdkit_smiles
from app.utils.exceptions import ModelArtifactError


SMILES_VOCAB = [
    "<PAD>",
    "<SOS>",
    "<EOS>",
    "<UNK>",
    "C",
    "c",
    "N",
    "n",
    "O",
    "o",
    "F",
    "S",
    "s",
    "Cl",
    "H",
    "*",
    "(",
    ")",
    "[",
    "]",
    "=",
    "#",
    ".",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "0",
    "/",
    "\\",
    "@",
    "+",
    "-",
]
MAX_SMILES_LEN = 100
EMBED_DIM = 128
CHEMPROP_HIDDEN_DIM = 300
TRANSFORMER_LAYERS = 4
N_HEADS = 8
DROPOUT = 0.1


class SMILESTokenizer:
    def __init__(self) -> None:
        self.vocab = SMILES_VOCAB
        self.char_to_id = {char: index for index, char in enumerate(self.vocab)}
        self.id_to_char = {index: char for index, char in enumerate(self.vocab)}
        self.pad_id = self.char_to_id["<PAD>"]
        self.sos_id = self.char_to_id["<SOS>"]
        self.eos_id = self.char_to_id["<EOS>"]
        self.unk_id = self.char_to_id["<UNK>"]

    def decode(self, ids: list[int] | Any) -> str:
        chars: list[str] = []
        for value in ids:
            index = value.item() if hasattr(value, "item") else int(value)
            if index == self.eos_id:
                break
            if index in {self.sos_id, self.pad_id}:
                continue
            chars.append(self.id_to_char.get(index, ""))
        return "".join(chars)


def _require_dependency(name: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ModelArtifactError(f"conditional generation dependency is not installed: {name}") from exc


def _cuda_support_error(torch_module) -> str | None:
    if not torch_module.cuda.is_available():
        return "CUDA is not available"

    try:
        device_major, device_minor = torch_module.cuda.get_device_capability()
        supported_arches = torch_module.cuda.get_arch_list()
    except Exception:
        return None

    supported_capabilities: list[tuple[int, int]] = []
    for arch in supported_arches:
        if not isinstance(arch, str) or not arch.startswith("sm_"):
            continue
        code = arch.removeprefix("sm_")
        if len(code) < 2 or not code.isdigit():
            continue
        supported_capabilities.append((int(code[:-1]), int(code[-1])))

    if not supported_capabilities:
        return None

    device_supported = any(
        device_major == supported_major and device_minor >= supported_minor
        for supported_major, supported_minor in supported_capabilities
    )
    if device_supported:
        return None

    supported_text = ", ".join(f"sm_{major}{minor}" for major, minor in supported_capabilities)
    return (
        f"CUDA device capability sm_{device_major}{device_minor} is not supported by "
        f"this PyTorch build ({supported_text})"
    )


def _resolve_device(torch_module, device_setting: str) -> str:
    value = device_setting.strip().lower()
    if value == "auto":
        return "cpu" if _cuda_support_error(torch_module) else "cuda"
    if value == "cuda":
        support_error = _cuda_support_error(torch_module)
        if support_error:
            raise ModelArtifactError(f"GEN_DEVICE=cuda was requested but {support_error}")
    if value not in {"cpu", "cuda"}:
        raise ModelArtifactError("GEN_DEVICE must be one of: auto, cpu, cuda")
    return value


def _build_model_classes(
    torch_module,
    nn_module,
    auto_model_class,
    simple_featurizer_class,
    batch_mol_graph_class,
    bond_message_passing_class,
    mean_aggregation_class,
    tokenizer: SMILESTokenizer,
):
    class PositionalEncoding(nn_module.Module):
        def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 500) -> None:
            super().__init__()
            self.dropout = nn_module.Dropout(p=dropout)
            pe = torch_module.zeros(max_len, d_model)
            position = torch_module.arange(0, max_len, dtype=torch_module.float).unsqueeze(1)
            div_term = torch_module.exp(
                torch_module.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model)
            )
            pe[:, 0::2] = torch_module.sin(position * div_term)
            pe[:, 1::2] = torch_module.cos(position * div_term)
            pe = pe.unsqueeze(0)
            self.register_buffer("pe", pe)

        def forward(self, x):
            x = x + self.pe[:, : x.size(1), :]
            return self.dropout(x)

    class ChempropPolymerEncoder(nn_module.Module):
        def __init__(
            self,
            embed_dim: int = EMBED_DIM,
            hidden_dim: int = CHEMPROP_HIDDEN_DIM,
            num_layers: int = 3,
        ) -> None:
            super().__init__()
            self.featurizer = simple_featurizer_class()
            dummy_mol = Chem.MolFromSmiles("CC")
            dummy_graph = self.featurizer(dummy_mol)
            d_v = dummy_graph.V.shape[1]
            d_e = dummy_graph.E.shape[1]
            self.message_passing = bond_message_passing_class(
                d_v=d_v,
                d_e=d_e,
                d_h=hidden_dim,
                depth=num_layers,
            )
            self.agg = mean_aggregation_class()
            self.fc_out = nn_module.Sequential(
                nn_module.Linear(hidden_dim, hidden_dim),
                nn_module.ReLU(),
                nn_module.Linear(hidden_dim, embed_dim),
            )

        def forward(self, smiles_list):
            mol_graphs = []
            for smiles in smiles_list:
                rdkit_smiles = to_rdkit_smiles(smiles)
                mol = Chem.MolFromSmiles(rdkit_smiles) if rdkit_smiles is not None else None
                mol_graphs.append(self.featurizer(mol or Chem.MolFromSmiles("C")))

            batch_graph = batch_mol_graph_class(mol_graphs)
            device = self.fc_out[0].weight.device
            batch_graph.V = batch_graph.V.to(device)
            batch_graph.E = batch_graph.E.to(device)
            batch_graph.edge_index = batch_graph.edge_index.to(device)
            batch_graph.rev_edge_index = batch_graph.rev_edge_index.to(device)
            batch_graph.batch = batch_graph.batch.to(device)
            h_v = self.message_passing(batch_graph)
            h_graph = self.agg(h_v, batch_graph.batch)
            return self.fc_out(h_graph)

    class SMILESTransformerDecoder(nn_module.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embed_dim = EMBED_DIM
            self.embedding = nn_module.Embedding(len(SMILES_VOCAB), EMBED_DIM, padding_idx=tokenizer.pad_id)
            self.pos_encoder = PositionalEncoding(EMBED_DIM, DROPOUT)
            decoder_layer = nn_module.TransformerDecoderLayer(
                d_model=EMBED_DIM,
                nhead=N_HEADS,
                dim_feedforward=EMBED_DIM * 4,
                dropout=DROPOUT,
                activation="gelu",
                norm_first=True,
                batch_first=True,
            )
            self.transformer_decoder = nn_module.TransformerDecoder(
                decoder_layer,
                num_layers=TRANSFORMER_LAYERS,
            )
            self.fc_out = nn_module.Linear(EMBED_DIM, len(SMILES_VOCAB))
            self.fc_out.weight = self.embedding.weight
            self._reset_parameters()

        def _reset_parameters(self) -> None:
            for parameter in self.parameters():
                if parameter.dim() > 1:
                    nn_module.init.xavier_uniform_(parameter)

        def generate_square_subsequent_mask(self, size: int):
            mask = (torch_module.triu(torch_module.ones(size, size)) == 1).transpose(0, 1)
            return mask.float().masked_fill(mask == 0, float("-inf")).masked_fill(mask == 1, float(0.0))

        def forward(self, tgt_seq, memory):
            seq_len = tgt_seq.size(1)
            tgt_mask = self.generate_square_subsequent_mask(seq_len).to(tgt_seq.device)
            tgt_key_padding_mask = (tgt_seq == tokenizer.pad_id).to(tgt_seq.device)
            tgt_emb = self.embedding(tgt_seq) * math.sqrt(self.embed_dim)
            tgt_emb = self.pos_encoder(tgt_emb)
            if memory.dim() == 2:
                memory = memory.unsqueeze(1)
            output = self.transformer_decoder(
                tgt=tgt_emb,
                memory=memory,
                tgt_mask=tgt_mask,
                tgt_key_padding_mask=tgt_key_padding_mask,
            )
            return self.fc_out(output)

    class ChempropTransformerAE(nn_module.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = ChempropPolymerEncoder()
            self.decoder = SMILESTransformerDecoder()
            self.condition_embedding = nn_module.Sequential(
                nn_module.Linear(1, 64),
                nn_module.GELU(),
                nn_module.Linear(64, EMBED_DIM),
            )
            for module in self.condition_embedding.modules():
                if isinstance(module, nn_module.Linear):
                    nn_module.init.xavier_uniform_(module.weight)
                    if module.bias is not None:
                        nn_module.init.constant_(module.bias, 0)

    class ChemBERTaTgModel(nn_module.Module):
        def __init__(self, model_name: str, num_desc: int) -> None:
            super().__init__()
            self.chemberta = auto_model_class.from_pretrained(model_name)
            self.desc_mlp = nn_module.Sequential(
                nn_module.Linear(num_desc, 32),
                nn_module.ReLU(),
                nn_module.LayerNorm(32),
            )
            self.regression_head = nn_module.Sequential(
                nn_module.Linear(self.chemberta.config.hidden_size + 32, 256),
                nn_module.ReLU(),
                nn_module.Dropout(0.4),
                nn_module.Linear(256, 64),
                nn_module.ReLU(),
                nn_module.Linear(64, 1),
            )

        def forward(self, input_ids, attention_mask, descriptors):
            emb = self.chemberta(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state[:, 0, :]
            combined = torch_module.cat([emb, self.desc_mlp(descriptors)], dim=1)
            return self.regression_head(combined)

    return ChempropTransformerAE, ChemBERTaTgModel


class TorchConditionalGenerationRuntime:
    def __init__(self, *, model_dir: Path, device: str = "auto") -> None:
        self.model_dir = model_dir
        self.device_setting = device
        self.tokenizer = SMILESTokenizer()
        self.device: str | None = None
        self.generator_model = None
        self.cond_mean = 0.0
        self.cond_std = 1.0
        self.evaluator_bundle: dict[str, Any] | None = None
        self.torch = None
        self.functional = None
        self._load_lock = Lock()

    def _assert_artifacts(self) -> None:
        required = [
            self.model_dir / "generator_best_40.pth",
            self.model_dir / "best_chemberta_tg.pth",
            self.model_dir / "top10_desc_names.pkl",
            self.model_dir / "tg_scaler.pkl",
            self.model_dir / "ChemBerta" / "config.json",
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ModelArtifactError("conditional generation artifacts are missing: " + ", ".join(missing))

    def _load(self) -> None:
        if self.generator_model is not None and self.evaluator_bundle is not None:
            return

        with self._load_lock:
            if self.generator_model is not None and self.evaluator_bundle is not None:
                return
            self._load_unlocked()

    def _load_unlocked(self) -> None:
        self._assert_artifacts()
        torch_module = _require_dependency("torch")
        nn_module = torch_module.nn
        self.functional = _require_dependency("torch.nn.functional")
        transformers = _require_dependency("transformers")
        chemprop_featurizers = _require_dependency("chemprop.featurizers")
        chemprop_data = _require_dependency("chemprop.data")
        chemprop_nn = _require_dependency("chemprop.nn")

        self.torch = torch_module
        self.device = _resolve_device(torch_module, self.device_setting)
        gc.collect()
        if torch_module.cuda.is_available():
            torch_module.cuda.empty_cache()

        ChempropTransformerAE, ChemBERTaTgModel = _build_model_classes(
            torch_module,
            nn_module,
            transformers.AutoModel,
            chemprop_featurizers.SimpleMoleculeMolGraphFeaturizer,
            chemprop_data.BatchMolGraph,
            chemprop_nn.BondMessagePassing,
            chemprop_nn.MeanAggregation,
            self.tokenizer,
        )

        generator_model = ChempropTransformerAE().to(self.device)
        ckpt = torch_module.load(self.model_dir / "generator_best_40.pth", map_location=self.device)
        state = ckpt["model_state_dict"] if isinstance(ckpt, dict) and "model_state_dict" in ckpt else ckpt
        generator_model.load_state_dict(state, strict=False)
        generator_model.eval()
        self.generator_model = generator_model
        self.cond_mean = float(ckpt.get("cond_mean", 0.0)) if isinstance(ckpt, dict) else 0.0
        self.cond_std = float(ckpt.get("cond_std", 1.0)) if isinstance(ckpt, dict) else 1.0

        top_10_features = joblib.load(self.model_dir / "top10_desc_names.pkl")
        scaler = joblib.load(self.model_dir / "tg_scaler.pkl")
        chemberta_path = str(self.model_dir / "ChemBerta")
        hf_tokenizer = transformers.AutoTokenizer.from_pretrained(chemberta_path)
        evaluator_model = ChemBERTaTgModel(model_name=chemberta_path, num_desc=len(top_10_features)).to(self.device)
        evaluator_ckpt = torch_module.load(self.model_dir / "best_chemberta_tg.pth", map_location=self.device)
        evaluator_state = (
            evaluator_ckpt["model_state_dict"]
            if isinstance(evaluator_ckpt, dict) and "model_state_dict" in evaluator_ckpt
            else evaluator_ckpt
        )
        evaluator_model.load_state_dict(evaluator_state, strict=False)
        evaluator_model.eval()
        self.evaluator_bundle = {
            "model": evaluator_model,
            "tokenizer": hf_tokenizer,
            "top_10_features": top_10_features,
            "scaler": scaler,
        }

    def generate_once(
        self,
        *,
        input_smiles: str,
        delta_tg: float,
        top_k: int,
        temperature: float,
        max_length: int,
    ) -> GeneratedSmiles:
        self._load()
        assert self.torch is not None
        assert self.functional is not None
        assert self.generator_model is not None
        assert self.device is not None

        model_input_smiles = to_model_smiles(input_smiles)
        if model_input_smiles is None:
            return GeneratedSmiles(raw_smiles="", rdkit_smiles=None)

        with self.torch.no_grad():
            z_graph = self.generator_model.encoder([model_input_smiles])
            norm_cond = 0.0 if abs(self.cond_std) < 1e-12 else (float(delta_tg) - self.cond_mean) / (self.cond_std + 1e-8)
            cond_tensor = self.torch.tensor([[norm_cond]], dtype=self.torch.float32, device=self.device)
            cond_emb = self.generator_model.condition_embedding(cond_tensor)
            memory = self.torch.cat([z_graph.unsqueeze(1), cond_emb.unsqueeze(1)], dim=1)
            generated_ids = [self.tokenizer.sos_id]

            for _ in range(max_length or MAX_SMILES_LEN):
                input_tensor = self.torch.tensor([generated_ids], dtype=self.torch.long, device=self.device)
                logits = self.generator_model.decoder(input_tensor, memory)
                next_token_logits = logits[0, -1, :] / max(float(temperature), 1e-8)
                probs = self.functional.softmax(next_token_logits, dim=-1)
                k = min(int(top_k), probs.size(-1))
                top_k_probs, top_k_indices = self.torch.topk(probs, k)
                top_k_probs = top_k_probs / self.torch.sum(top_k_probs)
                choice = self.torch.multinomial(top_k_probs, num_samples=1).item()
                next_token = top_k_indices[choice].item()
                if next_token == self.tokenizer.eos_id:
                    break
                generated_ids.append(next_token)

        raw_smiles = self.tokenizer.decode(generated_ids)
        return GeneratedSmiles(raw_smiles=raw_smiles, rdkit_smiles=to_rdkit_smiles(raw_smiles))

    def predict_tg(self, smiles: str) -> float:
        self._load()
        assert self.torch is not None
        assert self.device is not None
        assert self.evaluator_bundle is not None

        rdkit_smiles = to_rdkit_smiles(smiles)
        if rdkit_smiles is None:
            raise ValueError("invalid smiles")

        top_10_features = self.evaluator_bundle["top_10_features"]
        descriptor_values = _descriptor_values(rdkit_smiles, top_10_features)
        scaled_desc = self.evaluator_bundle["scaler"].transform(np.array([descriptor_values], dtype=np.float32))
        encodings = self.evaluator_bundle["tokenizer"](
            [rdkit_smiles],
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )

        with self.torch.no_grad():
            preds = self.evaluator_bundle["model"](
                input_ids=encodings["input_ids"].to(self.device),
                attention_mask=encodings["attention_mask"].to(self.device),
                descriptors=self.torch.tensor(scaled_desc, dtype=self.torch.float32, device=self.device),
            )

        predicted_kelvin = float(preds.detach().cpu().numpy().reshape(-1)[0] + 273.15)
        return predicted_kelvin - 273.15


def _descriptor_values(smiles: str, feature_names: list[str]) -> np.ndarray:
    rdkit_smiles = to_rdkit_smiles(smiles)
    mol = Chem.MolFromSmiles(rdkit_smiles) if rdkit_smiles is not None else None
    if mol is None:
        raise ValueError("invalid smiles")

    descriptor_map = dict(Descriptors.descList)
    values: list[float] = []
    for name in feature_names:
        if name not in descriptor_map:
            raise ModelArtifactError(f"RDKit descriptor is not available: {name}")
        values.append(float(descriptor_map[name](mol)))
    return np.array(values, dtype=np.float32)
