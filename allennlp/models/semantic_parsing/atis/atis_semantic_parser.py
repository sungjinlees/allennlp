from typing import Any, Dict, List, Tuple
import re

from overrides import overrides
import torch

from allennlp.common import Params
from allennlp.data import Vocabulary
from allennlp.data.fields.production_rule_field import ProductionRuleArray
from allennlp.models.model import Model
from allennlp.models.semantic_parsing.wikitables.wikitables_decoder_step import WikiTablesDecoderStep
from allennlp.models.semantic_parsing.wikitables.wikitables_semantic_parser import WikiTablesSemanticParser
from allennlp.modules import Attention, FeedForward, Seq2SeqEncoder, Seq2VecEncoder, TextFieldEmbedder
from allennlp.nn.decoding import BeamSearch
from allennlp.nn.decoding.decoder_trainers import MaximumMarginalLikelihood
from allennlp.semparse.worlds import WikiTablesWorld
from allennlp.semparse import ParsingError

from allennlp.common.checks import check_dimensions_match
from allennlp.common.util import pad_sequence_to_length
from allennlp.data import Vocabulary
from allennlp.data.fields.production_rule_field import ProductionRuleArray
from allennlp.models.model import Model
from allennlp.models.semantic_parsing.wikitables.wikitables_decoder_state import WikiTablesDecoderState
from allennlp.modules import Embedding, Seq2SeqEncoder, Seq2VecEncoder, TextFieldEmbedder, TimeDistributed
from allennlp.modules.seq2vec_encoders import BagOfEmbeddingsEncoder
from allennlp.nn import util
from allennlp.nn.decoding import GrammarState, RnnState, ChecklistState, AtisGrammarState, is_nonterminal
from allennlp.semparse.type_declarations import type_declaration
from allennlp.semparse.type_declarations.type_declaration import START_SYMBOL
from allennlp.semparse.worlds import WikiTablesWorld, AtisWorld
from allennlp.training.metrics import Average, WikiTablesAccuracy

@Model.register("atis_parser")
class AtisSemanticParser(Model):
    """
    Parameters
    ----------
    vocab : ``Vocabulary``
    utterance_embedder : ``TextFieldEmbedder``
        Embedder for utterances.
    action_embedding_dim : ``int``
        Dimension to use for action embeddings.
    encoder : ``Seq2SeqEncoder``
        The encoder to use for the input utterance.
    entity_encoder : ``Seq2VecEncoder``
        The encoder to used for averaging the words of an entity.
    max_decoding_steps : ``int``
        When we're decoding with a beam search, what's the maximum number of steps we should take?
        This only applies at evaluation time, not during training.
    add_action_bias : ``bool``, optional (default=True)
        If ``True``, we will learn a bias weight for each action that gets used when predicting
        that action, in addition to its embedding.
    use_neighbor_similarity_for_linking : ``bool``, optional (default=False)
        If ``True``, we will compute a max similarity between a utterance token and the `neighbors`
        of an entity as a component of the linking scores.  This is meant to capture the same kind
        of information as the ``related_column`` feature.
    dropout : ``float``, optional (default=0)
        If greater than 0, we will apply dropout with this probability after all encoders (pytorch
        LSTMs do not apply dropout to their last layer).
    num_linking_features : ``int``, optional (default=10)
        We need to construct a parameter vector for the linking features, so we need to know how
        many there are.  The default of 8 here matches the default in the ``KnowledgeGraphField``,
        which is to use all eight defined features. If this is 0, another term will be added to the
        linking score. This term contains the maximum similarity value from the entity's neighbors
        and the utterance.
    rule_namespace : ``str``, optional (default=rule_labels)
        The vocabulary namespace to use for production rules.  The default corresponds to the
        default used in the dataset reader, so you likely don't need to modify this.
    tables_directory : ``str``, optional (default=/wikitables/)
        The directory to find tables when evaluating logical forms.  We rely on a call to SEMPRE to
        evaluate logical forms, and SEMPRE needs to read the table from disk itself.  This tells
        SEMPRE where to find the tables.
    """
    # pylint: disable=abstract-method
    def __init__(self,
                 vocab: Vocabulary,
                 utterance_embedder: TextFieldEmbedder,
                 action_embedding_dim: int,
                 encoder: Seq2SeqEncoder,
                 entity_encoder: Seq2VecEncoder,
                 mixture_feedforward: FeedForward,
                 decoder_beam_search: BeamSearch,
                 max_decoding_steps: int,
                 input_attention: Attention,
                 add_action_bias: bool = True,
                 training_beam_size: int = None,
                 use_neighbor_similarity_for_linking: bool = False,
                 dropout: float = 0.0,
                 num_linking_features: int = 10,
                 rule_namespace: str = 'rule_labels',
                 tables_directory: str = '/wikitables/') -> None:
        use_similarity = use_neighbor_similarity_for_linking


        # Atis semantic parser init
        super(AtisSemanticParser, self).__init__(vocab)
        self._utterance_embedder = utterance_embedder
        self._encoder = encoder
        self._entity_encoder = TimeDistributed(entity_encoder)
        self._max_decoding_steps = max_decoding_steps
        self._add_action_bias = add_action_bias
        self._use_neighbor_similarity_for_linking = use_neighbor_similarity_for_linking
        if dropout > 0:
            self._dropout = torch.nn.Dropout(p=dropout)
        else:
            self._dropout = lambda x: x
        self._rule_namespace = rule_namespace
        # self._denotation_accuracy = WikiTablesAccuracy(tables_directory) This runs a SEMPRE server
        self._action_sequence_accuracy = Average()
        self._has_logical_form = Average()

        self._action_padding_index = -1  # the padding value used by IndexField
        num_actions = vocab.get_vocab_size(self._rule_namespace)
        if self._add_action_bias:
            input_action_dim = action_embedding_dim + 1
        else:
            input_action_dim = action_embedding_dim
        self._action_embedder = Embedding(num_embeddings=num_actions, embedding_dim=input_action_dim)
        self._output_action_embedder = Embedding(num_embeddings=num_actions, embedding_dim=action_embedding_dim)


        # This is what we pass as input in the first step of decoding, when we don't have a
        # previous action, or a previous utterance attention.
        self._first_action_embedding = torch.nn.Parameter(torch.FloatTensor(action_embedding_dim))
        self._first_attended_utterance = torch.nn.Parameter(torch.FloatTensor(encoder.get_output_dim()))
        torch.nn.init.normal_(self._first_action_embedding)
        torch.nn.init.normal_(self._first_attended_utterance)

        check_dimensions_match(entity_encoder.get_output_dim(), utterance_embedder.get_output_dim(),
                               "entity word average embedding dim", "utterance embedding dim")

        self._num_entity_types = 4  # TODO(mattg): get this in a more principled way somehow?
        self._num_start_types = 1  # TODO(mattg): get this in a more principled way somehow? Only one type in SQL
        self._embedding_dim = utterance_embedder.get_output_dim()
        # self._entity_type_encoder_embedding = Embedding(self._num_entity_types, self._embedding_dim)
        self._entity_type_decoder_embedding = Embedding(self._num_entity_types, action_embedding_dim)
        # self._neighbor_params = torch.nn.Linear(self._embedding_dim, self._embedding_dim)
        
        '''
        if num_linking_features > 0:
            self._linking_params = torch.nn.Linear(num_linking_features, 1)
        else:
            self._linking_params = None

        if self._use_neighbor_similarity_for_linking:
            self._utterance_entity_params = torch.nn.Linear(1, 1)
            self._utterance_neighbor_params = torch.nn.Linear(1, 1)
        else:
            self._utterance_entity_params = None
            self._utterance_neighbor_params = None
        '''

        # The rest of Wikitables MML
        self._beam_search = decoder_beam_search
        self._decoder_trainer = MaximumMarginalLikelihood(training_beam_size)
        self._decoder_step = WikiTablesDecoderStep(encoder_output_dim=self._encoder.get_output_dim(),
                                                   action_embedding_dim=action_embedding_dim,
                                                   input_attention=input_attention,
                                                   num_start_types=self._num_start_types,
                                                   predict_start_type_separately=False,
                                                   add_action_bias=self._add_action_bias,
                                                   mixture_feedforward=mixture_feedforward,
                                                   dropout=dropout)





    def _get_initial_state_and_scores(self,
                                      utterance: Dict[str, torch.LongTensor],
                                      # table: Dict[str, torch.LongTensor],
                                      world: List[Dict[str, List[str]]],
                                      actions: List[List[ProductionRuleArray]],
                                      example_lisp_string: List[str] = None,
                                      add_world_to_initial_state: bool = False,
                                      checklist_states: List[ChecklistState] = None) -> Dict:
        """
        Does initial preparation and creates an intiial state for both the semantic parsers. Note
        that the checklist state is optional, and the ``WikiTablesMmlParser`` is not expected to
        pass it.
        """

        # table_text = table['text']
        # (batch_size, utterance_length, embedding_dim)
        embedded_utterance = self._utterance_embedder(utterance)
        utterance_mask = util.get_text_field_mask(utterance).float()
        # (batch_size, num_entities, num_entity_tokens, embedding_dim)
        # embedded_table = self._utterance_embedder(table_text, num_wrapping_dims=1)
        # table_mask = util.get_text_field_mask(table_text, num_wrapping_dims=1).float()

        # batch_size, num_entities, num_entity_tokens, _ = embedded_table.size()
        num_utterance_tokens = embedded_utterance.size(1)
        batch_size = embedded_utterance.size(0) 

        '''
        # (batch_size, num_entities, embedding_dim)
        encoded_table = self._entity_encoder(embedded_table, table_mask)
        # (batch_size, num_entities, num_neighbors)
        neighbor_indices = self._get_neighbor_indices(world, num_entities, encoded_table)

        # Neighbor_indices is padded with -1 since 0 is a potential neighbor index.
        # Thus, the absolute value needs to be taken in the index_select, and 1 needs to
        # be added for the mask since that method expects 0 for padding.
        # (batch_size, num_entities, num_neighbors, embedding_dim)
        embedded_neighbors = util.batched_index_select(encoded_table, torch.abs(neighbor_indices))

        neighbor_mask = util.get_text_field_mask({'ignored': neighbor_indices + 1},
                                                 num_wrapping_dims=1).float()

        # Encoder initialized to easily obtain a masked average.
        neighbor_encoder = TimeDistributed(BagOfEmbeddingsEncoder(self._embedding_dim, averaged=True))
        # (batch_size, num_entities, embedding_dim)
        embedded_neighbors = neighbor_encoder(embedded_neighbors, neighbor_mask)

        # entity_types: one-hot tensor with shape (batch_size, num_entities, num_types)
        # entity_type_dict: Dict[int, int], mapping flattened_entity_index -> type_index
        # These encode the same information, but for efficiency reasons later it's nice
        # to have one version as a tensor and one that's accessible on the cpu.
        entity_types, entity_type_dict = self._get_type_vector(world, num_entities, encoded_table)
        '''

        # entity_type_embeddings = self._entity_type_encoder_embedding(entity_types)

        '''
        projected_neighbor_embeddings = self._neighbor_params(embedded_neighbors.float())
        # (batch_size, num_entities, embedding_dim)
        entity_embeddings = torch.nn.functional.tanh(entity_type_embeddings + projected_neighbor_embeddings)


        # Compute entity and utterance word similarity.  We tried using cosine distance here, but
        # because this similarity is the main mechanism that the model can use to push apart logit
        # scores for certain actions (like "n -> 1" and "n -> -1"), this needs to have a larger
        # output range than [-1, 1].
        utterance_entity_similarity = torch.bmm(embedded_table.view(batch_size,
                                                                   num_entities * num_entity_tokens,
                                                                   self._embedding_dim),
                                               torch.transpose(embedded_utterance, 1, 2))

        utterance_entity_similarity = utterance_entity_similarity.view(batch_size,
                                                                     num_entities,
                                                                     num_entity_tokens,
                                                                     num_utterance_tokens)

        # (batch_size, num_entities, num_utterance_tokens)
        utterance_entity_similarity_max_score, _ = torch.max(utterance_entity_similarity, 2)

        # (batch_size, num_entities, num_utterance_tokens, num_features)
        linking_features = table['linking']

        linking_scores = utterance_entity_similarity_max_score

        if self._use_neighbor_similarity_for_linking:
            # The linking score is computed as a linear projection of two terms. The first is the
            # maximum similarity score over the entity's words and the utterance token. The second
            # is the maximum similarity over the words in the entity's neighbors and the utterance
            # token.
            #
            # The second term, projected_utterance_neighbor_similarity, is useful when a column
            # needs to be selected. For example, the utterance token might have no similarity with
            # the column name, but is similar with the cells in the column.
            #
            # Note that projected_utterance_neighbor_similarity is intended to capture the same
            # information as the related_column feature.
            #
            # Also note that this block needs to be _before_ the `linking_params` block, because
            # we're overwriting `linking_scores`, not adding to it.

            # (batch_size, num_entities, num_neighbors, num_utterance_tokens)
            utterance_neighbor_similarity = util.batched_index_select(utterance_entity_similarity_max_score,
                                                                     torch.abs(neighbor_indices))
            # (batch_size, num_entities, num_utterance_tokens)
            utterance_neighbor_similarity_max_score, _ = torch.max(utterance_neighbor_similarity, 2)
            projected_utterance_entity_similarity = self._utterance_entity_params(
                    utterance_entity_similarity_max_score.unsqueeze(-1)).squeeze(-1)
            projected_utterance_neighbor_similarity = self._utterance_neighbor_params(
                    utterance_neighbor_similarity_max_score.unsqueeze(-1)).squeeze(-1)
            linking_scores = projected_utterance_entity_similarity + projected_utterance_neighbor_similarity

        feature_scores = None
        if self._linking_params is not None:
            feature_scores = self._linking_params(linking_features).squeeze(3)
            linking_scores = linking_scores + feature_scores

        # (batch_size, num_utterance_tokens, num_entities)
        linking_probabilities = self._get_linking_probabilities(world, linking_scores.transpose(1, 2),
                                                                utterance_mask, entity_type_dict)

        # (batch_size, num_utterance_tokens, embedding_dim)
        link_embedding = util.weighted_sum(entity_embeddings, linking_probabilities)
        '''

        # encoder_input = torch.cat([link_embedding, embedded_utterance], 2)

        # TODO actually calculate the linking, just make dims work for now
        encoder_input = torch.cat([embedded_utterance, embedded_utterance], 2)

        # (batch_size, utterance_length, encoder_output_dim)
        encoder_outputs = self._dropout(self._encoder(encoder_input, utterance_mask))

        # This will be our initial hidden state and memory cell for the decoder LSTM.
        final_encoder_output = util.get_final_encoder_states(encoder_outputs,
                                                             utterance_mask,
                                                             self._encoder.is_bidirectional())
        memory_cell = encoder_outputs.new_zeros(batch_size, self._encoder.get_output_dim())

        initial_score = embedded_utterance.data.new_zeros(batch_size)

        # To make grouping states together in the decoder easier, we convert the batch dimension in
        # all of our tensors into an outer list.  For instance, the encoder outputs have shape
        # `(batch_size, utterance_length, encoder_output_dim)`.  We need to convert this into a list
        # of `batch_size` tensors, each of shape `(utterance_length, encoder_output_dim)`.  Then we
        # won't have to do any index selects, or anything, we'll just do some `torch.cat()`s.
        initial_score_list = [initial_score[i] for i in range(batch_size)]
        encoder_output_list = [encoder_outputs[i] for i in range(batch_size)]
        utterance_mask_list = [utterance_mask[i] for i in range(batch_size)]
        initial_rnn_state = []
        for i in range(batch_size):
            initial_rnn_state.append(RnnState(final_encoder_output[i],
                                              memory_cell[i],
                                              self._first_action_embedding,
                                              self._first_attended_utterance,
                                              encoder_output_list,
                                              utterance_mask_list))

        # Assumed to have shape ``(num_entities, num_utterance_tokens)`` (i.e., there is no batch
        num_utterance_tokens = embedded_utterance.size(1) 
        
        num_entities = 0
        for w in world:
            num_entities = max(len(w['string']) + len(w['number']), num_entities)

        linking_score = torch.ones((num_entities, num_utterance_tokens), dtype=torch.float, requires_grad=True) 

        # entity_types: one-hot tensor with shape (batch_size, num_entities, num_types)
        num_types = 2
        entity_types = torch.zeros((batch_size, num_entities), dtype=torch.long, requires_grad=True) 

        # entity_type_embeddings = self._entity_type_encoder_embedding(entity_types)


        initial_grammar_state = [self._create_grammar_state(world[i],
                                                            actions[i],
                                                            linking_score, # TODO actually make a linking score
                                                            entity_types[i])
                                     for i in range(batch_size)]
        

        initial_state_world = world if add_world_to_initial_state else None
        initial_state = WikiTablesDecoderState(batch_indices=list(range(batch_size)),
                                               action_history=[[] for _ in range(batch_size)],
                                               score=initial_score_list,
                                               rnn_state=initial_rnn_state,
                                               grammar_state=initial_grammar_state,
                                               possible_actions=actions,
                                               world=initial_state_world,
                                               example_lisp_string=example_lisp_string,
                                               checklist_state=checklist_states,
                                               debug_info=None)
        
        return {"initial_state": initial_state} 

    def _get_type_vector(worlds: List[AtisWorld],
                         num_entities: int,
                         tensor: torch.Tensor) -> Tuple[torch.LongTensor, Dict[int, int]]:
        """
        Produces the one hot encoding for each entity's type. In addition,
        a map from a flattened entity index to type is returned to combine
        entity type operations into one method.

        Parameters
        ----------
        worlds : ``List[WikiTablesWorld]``
        num_entities : ``int``
        tensor : ``torch.Tensor``
            Used for copying the constructed list onto the right device.

        Returns
        -------
        A ``torch.LongTensor`` with shape ``(batch_size, num_entities, num_types)``.
        entity_types : ``Dict[int, int]``
            This is a mapping from ((batch_index * num_entities) + entity_index) to entity type id.
        """
        entity_types = {}
        batch_types = []
        for batch_index, world in enumerate(worlds):
            types = []
            for entity_index, entity in enumerate(world.table_graph.entities):
                # We need numbers to be first, then cells, then parts, then row, because our
                # entities are going to be sorted.  We do a split by type and then a merge later,
                # and it relies on this sorting.
                if entity.startswith('fb:cell'):
                    entity_type = 1
                elif entity.startswith('fb:part'):
                    entity_type = 2
                elif entity.startswith('fb:row'):
                    entity_type = 3
                else:
                    entity_type = 0
                types.append(entity_type)

                # For easier lookups later, we're actually using a _flattened_ version
                # of (batch_index, entity_index) for the key, because this is how the
                # linking scores are stored.
                flattened_entity_index = batch_index * num_entities + entity_index
                entity_types[flattened_entity_index] = entity_type
            padded = pad_sequence_to_length(types, num_entities, lambda: 0)
            batch_types.append(padded)
        return tensor.new_tensor(batch_types, dtype=torch.long), entity_types
 

    def _get_linking_probabilities(self,
                                   worlds: List[WikiTablesWorld],
                                   linking_scores: torch.FloatTensor,
                                   utterance_mask: torch.LongTensor,
                                   entity_type_dict: Dict[int, int]) -> torch.FloatTensor:
        """
        Produces the probability of an entity given a utterance word and type. The logic below
        separates the entities by type since the softmax normalization term sums over entities
        of a single type.

        Parameters
        ----------
        worlds : ``List[WikiTablesWorld]``
        linking_scores : ``torch.FloatTensor``
            Has shape (batch_size, num_utterance_tokens, num_entities).
        utterance_mask: ``torch.LongTensor``
            Has shape (batch_size, num_utterance_tokens).
        entity_type_dict : ``Dict[int, int]``
            This is a mapping from ((batch_index * num_entities) + entity_index) to entity type id.

        Returns
        -------
        batch_probabilities : ``torch.FloatTensor``
            Has shape ``(batch_size, num_utterance_tokens, num_entities)``.
            Contains all the probabilities for an entity given a utterance word.
        """
        _, num_utterance_tokens, num_entities = linking_scores.size()
        batch_probabilities = []

        for batch_index, world in enumerate(worlds):
            all_probabilities = []
            num_entities_in_instance = 0

            # NOTE: The way that we're doing this here relies on the fact that entities are
            # implicitly sorted by their types when we sort them by name, and that numbers come
            # before "fb:cell", and "fb:cell" comes before "fb:row".  This is not a great
            # assumption, and could easily break later, but it should work for now.
            for type_index in range(self._num_entity_types):
                # This index of 0 is for the null entity for each type, representing the case where a
                # word doesn't link to any entity.
                entity_indices = [0]
                entities = world.table_graph.entities
                for entity_index, _ in enumerate(entities):
                    if entity_type_dict[batch_index * num_entities + entity_index] == type_index:
                        entity_indices.append(entity_index)

                if len(entity_indices) == 1:
                    # No entities of this type; move along...
                    continue

                # We're subtracting one here because of the null entity we added above.
                num_entities_in_instance += len(entity_indices) - 1

                # We separate the scores by type, since normalization is done per type.  There's an
                # extra "null" entity per type, also, so we have `num_entities_per_type + 1`.  We're
                # selecting from a (num_utterance_tokens, num_entities) linking tensor on _dimension 1_,
                # so we get back something of shape (num_utterance_tokens,) for each index we're
                # selecting.  All of the selected indices together then make a tensor of shape
                # (num_utterance_tokens, num_entities_per_type + 1).
                indices = linking_scores.new_tensor(entity_indices, dtype=torch.long)
                entity_scores = linking_scores[batch_index].index_select(1, indices)

                # We used index 0 for the null entity, so this will actually have some values in it.
                # But we want the null entity's score to be 0, so we set that here.
                entity_scores[:, 0] = 0

                # No need for a mask here, as this is done per batch instance, with no padding.
                type_probabilities = torch.nn.functional.softmax(entity_scores, dim=1)
                all_probabilities.append(type_probabilities[:, 1:])

            # We need to add padding here if we don't have the right number of entities.
            if num_entities_in_instance != num_entities:
                zeros = linking_scores.new_zeros(num_utterance_tokens,
                                                 num_entities - num_entities_in_instance)
                all_probabilities.append(zeros)

            # (num_utterance_tokens, num_entities)
            probabilities = torch.cat(all_probabilities, dim=1)
            batch_probabilities.append(probabilities)
        batch_probabilities = torch.stack(batch_probabilities, dim=0)
        return batch_probabilities * utterance_mask.unsqueeze(-1).float()

    @staticmethod
    def _action_history_match(predicted: List[int], targets: torch.LongTensor) -> int:
        # TODO(mattg): this could probably be moved into a FullSequenceMatch metric, or something.
        # Check if target is big enough to cover prediction (including start/end symbols)
        if len(predicted) > targets.size(1):
            return 0
        predicted_tensor = targets.new_tensor(predicted)
        targets_trimmed = targets[:, :len(predicted)]
        # Return 1 if the predicted sequence is anywhere in the list of targets.
        return torch.max(torch.min(targets_trimmed.eq(predicted_tensor), dim=1)[0]).item()

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        """
        We track three metrics here:

            1. dpd_acc, which is the percentage of the time that our best output action sequence is
            in the set of action sequences provided by DPD.  This is an easy-to-compute lower bound
            on denotation accuracy for the set of examples where we actually have DPD output.  We
            only score dpd_acc on that subset.

            2. denotation_acc, which is the percentage of examples where we get the correct
            denotation.  This is the typical "accuracy" metric, and it is what you should usually
            report in an experimental result.  You need to be careful, though, that you're
            computing this on the full data, and not just the subset that has DPD output (make sure
            you pass "keep_if_no_dpd=True" to the dataset reader, which we do for validation data,
            but not training data).

            3. lf_percent, which is the percentage of time that decoding actually produces a
            finished logical form.  We might not produce a valid logical form if the decoder gets
            into a repetitive loop, or we're trying to produce a super long logical form and run
            out of time steps, or something.
        """
        return {
                'dpd_acc': self._action_sequence_accuracy.get_metric(reset),
                # 'denotation_acc': self._denotation_accuracy.get_metric(reset),
                # 'lf_percent': self._has_logical_form.get_metric(reset),
                }

    def _create_grammar_state(self,
                              world: Dict[str, List[str]],
                              possible_actions: List[ProductionRuleArray],
                              linking_scores: torch.Tensor,
                              entity_types: torch.Tensor) -> GrammarState:
        """
        This method creates the GrammarState object that's used for decoding.  Part of creating
        that is creating the `valid_actions` dictionary, which contains embedded representations of
        all of the valid actions.  So, we create that here as well.

        The inputs to this method are for a `single instance in the batch`; none of the tensors we
        create here are batched.  We grab the global action ids from the input
        ``ProductionRuleArrays``, and we use those to embed the valid actions for every
        non-terminal type.  We use the input ``linking_scores`` for non-global actions.

        Parameters
        ----------
        world : ``WikiTablesWorld``
            From the input to ``forward`` for a single batch instance.
        possible_actions : ``List[ProductionRuleArray]``
            From the input to ``forward`` for a single batch instance.
        linking_scores : ``torch.Tensor``
            Assumed to have shape ``(num_entities, num_utterance_tokens)`` (i.e., there is no batch
            dimension).
        entity_types : ``torch.Tensor``
            Assumed to have shape ``(num_entities,)`` (i.e., there is no batch dimension).
        """

        action_map = {}
        for action_index, action in enumerate(possible_actions):
            action_string = action[0]
            action_map[action_string] = action_index

        
        entity_map = {}
        entities = world['string'] + world['number']


        for entity_index, entity in enumerate(entities):
            entity_map[entity] = entity_index

        valid_actions = world 
        translated_valid_actions = {}
        for key, action_strings in valid_actions.items():
            translated_valid_actions[key] = {}
            # `key` here is a non-terminal from the grammar, and `action_strings` are all the valid
            # productions of that non-terminal.  We'll first split those productions by global vs.
            # linked action.


            new_action_strings = []
            for action_string in action_strings:
                if key == 'string': 
                    new_action_strings.append('{} -> ["\'{}\'"]'.format(key, action_string))
                    
                elif key in ['asterisk', 'table_name', 'number']:
                    new_action_strings.append('{} -> ["{}"]'.format(key, action_string))
                
                else:
                    action_string = action_string.lstrip("(").rstrip(")")
                    new_action_strings.append("{} -> [{}]".format(key, ", ".join([tok for tok in re.split(" ws |ws | ws", action_string) if tok])))
           
            action_indices = [action_map[action_string] for action_string in new_action_strings]
            production_rule_arrays = [(possible_actions[index], index) for index in action_indices]
            global_actions = []
            linked_actions = []
            for production_rule_array, action_index in production_rule_arrays:
                if production_rule_array[1]:
                    global_actions.append((production_rule_array[2], action_index))
                else:
                    linked_actions.append((production_rule_array[0], action_index))

            if global_actions:
                # Then we get the ebmedded representations of the global actions.
                global_action_tensors, global_action_ids = zip(*global_actions)
                global_action_tensor = torch.cat(global_action_tensors, dim=0)
                global_input_embeddings = self._action_embedder(global_action_tensor)
                global_output_embeddings = self._output_action_embedder(global_action_tensor)
                translated_valid_actions[key]['global'] = (global_input_embeddings,
                                                           global_output_embeddings,
                                                           list(global_action_ids))
            # Then the representations of the linked actions.
            if linked_actions:
                linked_rules, linked_action_ids = zip(*linked_actions)
                entities = [rule.split(' -> ')[1].lstrip('\"\'[').rstrip(']\"\'') for rule in linked_rules]
                entity_ids = [entity_map[entity] for entity in entities]

                entity_linking_scores = linking_scores[entity_ids]
                

                entity_type_tensor = entity_types[entity_ids]
                entity_type_embeddings = self._entity_type_decoder_embedding(entity_type_tensor)
                translated_valid_actions[key]['linked'] = (entity_linking_scores,
                                                           entity_type_embeddings,
                                                           list(linked_action_ids))

        # Lastly, we need to also create embedded representations of context-specific actions.  In
        # this case, those are only variable productions, like "r -> x".
        context_actions = {}
        for action_id, action in enumerate(possible_actions):
            if action[0].endswith(" -> x"):
                input_embedding = self._action_embedder(action[2])
                output_embedding = self._output_action_embedder(action[2])
                context_actions[action[0]] = (input_embedding, output_embedding, action_id)

        return AtisGrammarState(['stmt'],
                            {},
                            translated_valid_actions,
                            context_actions,
                            is_nonterminal)

    @overrides
    def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        This method overrides ``Model.decode``, which gets called after ``Model.forward``, at test
        time, to finalize predictions.  This is (confusingly) a separate notion from the "decoder"
        in "encoder/decoder", where that decoder logic lives in ``WikiTablesDecoderStep``.

        This method trims the output predictions to the first end symbol, replaces indices with
        corresponding tokens, and adds a field called ``predicted_tokens`` to the ``output_dict``.
        """
        action_mapping = output_dict['action_mapping']
        best_actions = output_dict["best_action_sequence"]
        debug_infos = output_dict['debug_info']
        batch_action_info = []
        for batch_index, (predicted_actions, debug_info) in enumerate(zip(best_actions, debug_infos)):
            instance_action_info = []
            for predicted_action, action_debug_info in zip(predicted_actions, debug_info):
                action_info = {}
                action_info['predicted_action'] = predicted_action
                considered_actions = action_debug_info['considered_actions']
                probabilities = action_debug_info['probabilities']
                actions = []
                for action, probability in zip(considered_actions, probabilities):
                    if action != -1:
                        actions.append((action_mapping[(batch_index, action)], probability))
                actions.sort()
                considered_actions, probabilities = zip(*actions)
                action_info['considered_actions'] = considered_actions
                action_info['action_probabilities'] = probabilities
                action_info['utterance_attention'] = action_debug_info.get('utterance_attention', [])
                instance_action_info.append(action_info)
            batch_action_info.append(instance_action_info)
        output_dict["predicted_actions"] = batch_action_info
        return output_dict

    @overrides
    def forward(self,  # type: ignore
                utterance: Dict[str, torch.LongTensor],
                # table: Dict[str, torch.LongTensor],
                world: List[Dict[str, List[str]]],
                actions: List[List[ProductionRuleArray]],
                example_lisp_string: List[str] = None,
                target_action_sequence: torch.LongTensor = None) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        In this method we encode the table entities, link them to words in the utterance, then
        encode the utterance. Then we set up the initial state for the decoder, and pass that
        state off to either a DecoderTrainer, if we're training, or a BeamSearch for inference,
        if we're not.
        Parameters
        ----------
        utterance : Dict[str, torch.LongTensor]
           The output of ``TextField.as_array()`` applied on the utterance ``TextField``. This will
           be passed through a ``TextFieldEmbedder`` and then through an encoder.
        table : ``Dict[str, torch.LongTensor]``
            The output of ``KnowledgeGraphField.as_array()`` applied on the table
            ``KnowledgeGraphField``.  This output is similar to a ``TextField`` output, where each
            entity in the table is treated as a "token", and we will use a ``TextFieldEmbedder`` to
            get embeddings for each entity.
        world : ``List[WikiTablesWorld]``
            We use a ``MetadataField`` to get the ``World`` for each input instance.  Because of
            how ``MetadataField`` works, this gets passed to us as a ``List[WikiTablesWorld]``,
        actions : ``List[List[ProductionRuleArray]]``
            A list of all possible actions for each ``World`` in the batch, indexed into a
            ``ProductionRuleArray`` using a ``ProductionRuleField``.  We will embed all of these
            and use the embeddings to determine which action to take at each timestep in the
            decoder.
        example_lisp_string : ``List[str]``, optional (default=None)
            The example (lisp-formatted) string corresponding to the given input.  This comes
            directly from the ``.examples`` file provided with the dataset.  We pass this to SEMPRE
            when evaluating denotation accuracy; it is otherwise unused.
        target_action_sequences : torch.Tensor, optional (default=None)
           A list of possibly valid action sequences, where each action is an index into the list
           of possible actions.  This tensor has shape ``(batch_size, num_action_sequences,
           sequence_length)``.
        """
        # initial_info = self._get_initial_state_and_scores(utterance, table, world, actions)
        initial_info = self._get_initial_state_and_scores(utterance, world, actions)
        initial_state = initial_info["initial_state"]
        # linking_scores = initial_info["linking_scores"]
        # feature_scores = initial_info["feature_scores"]
        # similarity_scores = initial_info["similarity_scores"]
        batch_size = list(utterance.values())[0].size(0)
        
        if target_action_sequence is not None:
            # Remove the trailing dimension (from ListField[ListField[IndexField]]).
            # We only have one logical form so don't think to do this
            target_action_sequence = target_action_sequence.squeeze(-1)
            target_mask = target_action_sequence != self._action_padding_index
        else:
            target_mask = None

        if self.training:
            return self._decoder_trainer.decode(initial_state,
                                                self._decoder_step,
                                                (target_action_sequence, target_mask))
        else:
            # TODO(pradeep): Most of the functionality in this black can be moved to the super
            # class.
            action_mapping = {}
            for batch_index, batch_actions in enumerate(actions):
                for action_index, action in enumerate(batch_actions):
                    action_mapping[(batch_index, action_index)] = action[0]
            outputs: Dict[str, Any] = {'action_mapping': action_mapping}
            if target_action_sequence is not None:
                outputs['loss'] = self._decoder_trainer.decode(initial_state,
                                                               self._decoder_step,
                                                               (target_action_sequence, target_mask))['loss']
            num_steps = self._max_decoding_steps
            # This tells the state to start keeping track of debug info, which we'll pass along in
            # our output dictionary.
            initial_state.debug_info = [[] for _ in range(batch_size)]
            best_final_states = self._beam_search.search(num_steps,
                                                         initial_state,
                                                         self._decoder_step,
                                                         keep_final_unfinished_states=False)
            outputs['best_action_sequence'] = []
            outputs['debug_info'] = []
            outputs['entities'] = []
            # outputs['linking_scores'] = linking_scores
            '''
            if feature_scores is not None:
                outputs['feature_scores'] = feature_scores
            '''
            # outputs['similarity_scores'] = similarity_scores
            outputs['logical_form'] = []
            for i in range(batch_size):
                # Decoding may not have terminated with any completed logical forms, if `num_steps`
                # isn't long enough (or if the model is not trained enough and gets into an
                # infinite action loop).
                if i in best_final_states:
                    best_action_indices = best_final_states[i][0].action_history[0]
                    if target_action_sequence is not None:
                        # Use a Tensor, not a Variable, to avoid a memory leak.
                        targets = target_action_sequence[i].data
                        sequence_in_targets = 0
                        sequence_in_targets = self._action_history_match(best_action_indices, targets)
                        self._action_sequence_accuracy(sequence_in_targets)
                    action_strings = [action_mapping[(i, action_index)] for action_index in best_action_indices]
                    '''
                    try:
                        logical_form = world[i].get_logical_form(action_strings, add_var_function=False)
                        self._has_logical_form(1.0)
                    except ParsingError:
                        self._has_logical_form(0.0)
                        logical_form = 'Error producing logical form'
                    '''
                    if example_lisp_string:
                        self._denotation_accuracy(logical_form, example_lisp_string[i])
                    outputs['best_action_sequence'].append(action_strings)
                    # outputs['logical_form'].append(logical_form)
                    outputs['debug_info'].append(best_final_states[i][0].debug_info[0])  # type: ignore
                    # outputs['entities'].append(world[i].table_graph.entities)
                else:
                    outputs['logical_form'].append('')
                    self._has_logical_form(0.0)
                    if example_lisp_string:
                        self._denotation_accuracy(None, example_lisp_string[i])
            return outputs

    @classmethod
    def from_params(cls, vocab, params: Params) -> 'AtisSemanticParser':
        utterance_embedder = TextFieldEmbedder.from_params(vocab, params.pop("utterance_embedder"))
        action_embedding_dim = params.pop_int("action_embedding_dim")
        encoder = Seq2SeqEncoder.from_params(params.pop("encoder"))
        entity_encoder = Seq2VecEncoder.from_params(params.pop('entity_encoder'))
        max_decoding_steps = params.pop_int("max_decoding_steps")
        mixture_feedforward_type = params.pop('mixture_feedforward', None)
        if mixture_feedforward_type is not None:
            mixture_feedforward = FeedForward.from_params(mixture_feedforward_type)
        else:
            mixture_feedforward = None
        decoder_beam_search = BeamSearch.from_params(params.pop("decoder_beam_search"))
        input_attention = Attention.from_params(params.pop("attention"))
        training_beam_size = params.pop_int('training_beam_size', None)
        use_neighbor_similarity_for_linking = params.pop_bool('use_neighbor_similarity_for_linking', False)
        dropout = params.pop_float('dropout', 0.0)
        num_linking_features = params.pop_int('num_linking_features', 10)
        tables_directory = params.pop('tables_directory', '/wikitables/')
        rule_namespace = params.pop('rule_namespace', 'rule_labels')
        params.assert_empty(cls.__name__)
        return cls(vocab,
                   utterance_embedder=utterance_embedder,
                   action_embedding_dim=action_embedding_dim,
                   encoder=encoder,
                   entity_encoder=entity_encoder,
                   mixture_feedforward=mixture_feedforward,
                   decoder_beam_search=decoder_beam_search,
                   max_decoding_steps=max_decoding_steps,
                   input_attention=input_attention,
                   training_beam_size=training_beam_size,
                   use_neighbor_similarity_for_linking=use_neighbor_similarity_for_linking,
                   dropout=dropout,
                   num_linking_features=num_linking_features,
                   tables_directory=tables_directory,
                   rule_namespace=rule_namespace)


