
from typing import List, Tuple, Union
import numpy as np
import torch
from smiles_lstm.model.smiles_vocabulary import Vocabulary, SMILESTokenizer
from smiles_lstm.utils.misc import suppress_warnings, get_device


suppress_warnings()


class RNN(torch.nn.Module):
    
    def __init__(self, voc_size : int, layer_size : int=512, num_layers : int=3,
                 cell_type : str='lstm', embedding_layer_size : int=256,
                 dropout : float=0., layer_normalization : bool=False) -> None:
        
        super(RNN, self).__init__()

        self._layer_size           = layer_size
        self._embedding_layer_size = embedding_layer_size
        self._num_layers           = num_layers
        self._cell_type            = cell_type.lower()
        self._dropout              = dropout
        self._layer_normalization  = layer_normalization

        self._embedding = torch.nn.Embedding(voc_size,
                                             self._embedding_layer_size)
        if self._cell_type == "gru":
            self._rnn = torch.nn.GRU(self._embedding_layer_size,
                                     self._layer_size,
                                     num_layers=self._num_layers,
                                     dropout=self._dropout,
                                     batch_first=True)
        elif self._cell_type == "lstm":
            self._rnn = torch.nn.LSTM(self._embedding_layer_size,
                                      self._layer_size,
                                      num_layers=self._num_layers,
                                      dropout=self._dropout,
                                      batch_first=True)
        else:
            raise ValueError("Value of the parameter cell_type should be 'gru' or 'lstm'")
        self._linear = torch.nn.Linear(self._layer_size, voc_size)

    def forward(self, input_vector : torch.Tensor,
                hidden_state : Union[None, torch.Tensor]=None) ->                Tuple[torch.Tensor, torch.Tensor]:  
        
        batch_size, seq_size = input_vector.size()

        _device = get_device()
        if hidden_state is None:
            size = (self._num_layers, batch_size, self._layer_size)
            if self._cell_type == "gru":
                hidden_state = torch.zeros(*size, device=_device)
            else:
                hidden_state = [torch.zeros(*size, device=_device),
                                torch.zeros(*size, device=_device)]

        embedded_data                   = self._embedding(input_vector)  
        output_vector, hidden_state_out = self._rnn(embedded_data, hidden_state)

        if self._layer_normalization:
            output_vector = torch.nn.functional.layer_norm(output_vector,
                                                           output_vector.size()[1:])
        output_vector = output_vector.reshape(-1, self._layer_size)

        output_data = self._linear(output_vector).view(batch_size, seq_size, -1)
        return output_data, hidden_state_out

    def get_params(self) -> dict:
        
        return {
            "dropout"              : self._dropout,
            "layer_size"           : self._layer_size,
            "num_layers"           : self._num_layers,
            "cell_type"            : self._cell_type,
            "embedding_layer_size" : self._embedding_layer_size
        }


class SmilesLSTM():
    
    def __init__(self, vocabulary: Vocabulary, tokenizer : SMILESTokenizer,
                 network_params : Union[dict, None]=None,
                 max_sequence_length : int=256) -> None:
        
        self.vocabulary          = vocabulary
        self.tokenizer           = tokenizer
        self.max_sequence_length = max_sequence_length
        self._device             = get_device()

        if not isinstance(network_params, dict):
            network_params = {}

        self.network = RNN(len(self.vocabulary), **network_params)
        if self._device == "cuda":
            self.network.cuda()

        self._nll_loss = torch.nn.NLLLoss(reduction="none")

    @classmethod
    def load_from_file(cls, file_path : str, sampling_mode : bool=False):
        
        model = torch.load(file_path)
        if sampling_mode:
            model.network.eval()

        return model

    def save_state(self, path: str):
        
        torch.save(self, path)

    def likelihood_smiles(self, smiles : list) -> torch.Tensor:
        
        tokens    = [self.tokenizer.tokenize(smile) for smile in smiles]
        encoded   = [self.vocabulary.encode(token) for token in tokens]
        sequences = [
            torch.tensor(encode, dtype=torch.long, device=self._device) for encode in encoded
        ]

        def collate_fn(encoded_seqs : torch.Tensor) -> torch.Tensor:
            
            max_length   = max([seq.size(0) for seq in encoded_seqs])
            collated_arr = torch.zeros(len(encoded_seqs),
                                       max_length,
                                       dtype=torch.long,
                                       device=self._device)  
            for i, seq in enumerate(encoded_seqs):
                collated_arr[i, :seq.size(0)] = seq
            return collated_arr

        padded_sequences = collate_fn(sequences)
        return self.likelihood(padded_sequences)

    def likelihood(self, sequences : torch.Tensor) -> torch.Tensor:
        
        if self._device == "cuda":
            sequences = sequences.to("cuda")
        logits, _ = self.network(sequences[:, :-1])  
        log_probs = logits.log_softmax(dim=2)
        return self._nll_loss(log_probs.transpose(1, 2), sequences[:, 1:]).sum(dim=1)

    def sample_smiles(self, num : int=128, batch_size : int=128) -> Tuple[List, np.array]:
        
        batch_sizes = (
            [batch_size for _ in range(num // batch_size)] + [num % batch_size]
        )
        smiles_sampled = []
        likelihoods_sampled = []

        for size in batch_sizes:
            if not size:
                break
            seqs, likelihoods = self._sample(batch_size=size)
            smiles = [
                self.tokenizer.untokenize(self.vocabulary.decode(seq)) for seq in seqs.cpu().numpy()
            ]

            smiles_sampled.extend(smiles)
            likelihoods_sampled.append(likelihoods.data.cpu().numpy())

            del seqs, likelihoods
        return smiles_sampled, np.concatenate(likelihoods_sampled)

    def sample_sequences_and_smiles(self, batch_size : int=128) ->                                    Tuple[torch.Tensor, List, torch.Tensor]:
        
        seqs, likelihoods = self._sample(batch_size=batch_size)
        smiles = [
            self.tokenizer.untokenize(self.vocabulary.decode(seq)) for seq in seqs.cpu().numpy()
        ]
        return seqs, smiles, likelihoods

    
    def _sample(self, batch_size : int=128) -> Tuple[torch.Tensor, torch.Tensor]:
        
        start_token = torch.zeros(batch_size,
                                  dtype=torch.long,
                                  device=self._device)
        start_token[:] = self.vocabulary["^"]
        input_vector = start_token
        sequences = [
            self.vocabulary["^"] * torch.ones([batch_size, 1],
                                              dtype=torch.long,
                                              device=self._device)
        ]
        
        
        hidden_state = None
        nlls = torch.zeros(batch_size, device=self._device)
        for _ in range(self.max_sequence_length - 1):
            logits, hidden_state = self.network(input_vector.unsqueeze(1),
                                                hidden_state)
            logits        = logits.squeeze(1)
            probabilities = logits.softmax(dim=1)
            log_probs     = logits.log_softmax(dim=1)
            input_vector  = torch.multinomial(probabilities, 1).view(-1)
            sequences.append(input_vector.view(-1, 1))
            nlls += self._nll_loss(log_probs, input_vector)
            if input_vector.sum() == 0:
                break

        sequences = torch.cat(sequences, 1)
        return sequences.data, nlls
