import flair
from typing import List, Optional
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from flair.data import Corpus
from flair.datasets import ColumnCorpus
from flair.models import TARSTagger, SequenceTagger
from flair.embeddings import WordEmbeddings, StackedEmbeddings, FlairEmbeddings
from flair.data import Sentence
from flair.trainers import ModelTrainer
import pandas as pd
from entity_utils import create_data, create_data_new
import configparser
from jaseci.actions.live_actions import jaseci_action
import torch
import os
from pathlib import Path
config = configparser.ConfigParser()
flair.device = torch.device('cpu')


# 1. initialize each embedding we use
embedding_types = [

    # GloVe embeddings
    WordEmbeddings('glove'),

    # contextual string embeddings, forward
    FlairEmbeddings('news-forward'),

    # contextual string embeddings, backward
    FlairEmbeddings('news-backward'),
]

# embedding stack consists of Flair and GloVe embeddings
embeddings = StackedEmbeddings(embeddings=embedding_types)

device = torch.device("cpu")
# uncomment this if you wish to use GPU to train
# this is commented out because this causes issues with
# unittest on machines with GPU
# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# tagger = None


def init_model():
    """create a tagger as per the model provided through config"""

    global tagger, NER_MODEL_NAME, NER_LABEL_TYPE, MODEL_TYPE
    config.read(
        os.path.join(
            os.path.dirname(__file__),
            'config.cfg'
        )
    )
    NER_MODEL_NAME = config['TAGGER_MODEL']['NER_MODEL']
    NER_LABEL_TYPE = config['LABEL_TYPE']['NER']
    MODEL_TYPE = config['TAGGER_MODEL']['MODEL_TYPE']
    if MODEL_TYPE.lower() == "trfmodel" and NER_MODEL_NAME.lower() != "none":
        tagger = TARSTagger(embeddings=NER_MODEL_NAME)
    elif NER_MODEL_NAME.lower() != "none" and MODEL_TYPE.lower() in ["lstm", "gru"]:
        tagger = SequenceTagger.load(NER_MODEL_NAME)
    else:
        tagger = None
    print(f"loaded mode : [{NER_MODEL_NAME}]")


# initialize the tagger
init_model()


def train_entity():
    """
    funtion for training the model
    """
    global tagger, NER_MODEL_NAME, MODEL_TYPE
    # define columns
    columns = {0: 'text', 1: 'ner'}
    # directory where the data resides
    data_folder = 'train'
    # initializing the corpus
    corpus: Corpus = ColumnCorpus(data_folder, columns,
                                  train_file='train.txt')
    # make tag dictionary from the corpus
    tag_dictionary = corpus.make_tag_dictionary(tag_type=NER_LABEL_TYPE)

    # make the model aware of the desired set of labels from the new corpus
    # initialize sequence tagger
    try:
        if MODEL_TYPE.lower() in "trfmodel":
            tagger.add_and_switch_to_new_task(
                "ner_train", label_dictionary=tag_dictionary, label_type=NER_LABEL_TYPE)
        elif tagger is None and MODEL_TYPE.lower() in ["lstm", "gru"]:
            tagger = SequenceTagger(hidden_size=256,
                                    embeddings=embeddings,
                                    tag_dictionary=tag_dictionary,
                                    tag_type=NER_LABEL_TYPE,
                                    rnn_type=MODEL_TYPE)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(
            f"Model load error : {e}"))
    #  initialize the Sequence trainer with your corpus
    trainer = ModelTrainer(tagger, corpus)
    # train model
    trainer.train(base_path=f'train/{NER_MODEL_NAME}',  # path to store the model artifacts
                  learning_rate=0.02,
                  mini_batch_size=1,
                  max_epochs=10,
                  train_with_test=True,
                  train_with_dev=True
                  )
    #  Load trained Sequence Tagger model
    tagger = tagger.load(f'train/{NER_MODEL_NAME}/final-model.pt')
    torch.cuda.empty_cache()
    print("model training and loading completed")


# defining the api for entitydetection
@jaseci_action(act_group=['ent_ext'], allow_remote=True)
def entity_detection(text: str, ner_labels: Optional[List]=["PREDEFINED"]):
    """
    API for detectiing provided entity in text
    """
    global tagger, MODEL_TYPE
    if tagger is not None:
        if text:
            if ner_labels:
                if "none" in MODEL_TYPE.lower():
                    tagger.add_and_switch_to_new_task(
                        'Entity Detection Task', ner_labels, label_type=NER_LABEL_TYPE)
                sentence = Sentence(text)
                # predicting entities in the text
                tagger.predict(sentence)
                tagged_sentence = sentence.to_dict(NER_LABEL_TYPE)
                json_compatible_data = jsonable_encoder(tagged_sentence)
                #response_data_format = {
                #    "input_text": json_compatible_data["text"], "entities": []}
                response_data_format = {"entities": []}

                for json_data in json_compatible_data["entities"]:
                    temp_dict = {}
                    temp_dict["entity_text"] = json_data["text"]
                    temp_dict["entity_value"] = json_data["labels"][0]["_value"]
                    temp_dict["conf_score"] = json_data["labels"][0]["_score"]
                    temp_dict["start_pos"] = json_data["start_pos"]
                    temp_dict["end_pos"] = json_data["end_pos"]
                    response_data_format['entities'].append(temp_dict)
                return response_data_format
            else:
                raise HTTPException(status_code=404, detail=str(
                    "NER Labels are missing in request data"))
        else:
            raise HTTPException(status_code=404, detail=str(
                "Text data is missing in request data"))
    else:
        raise HTTPException(status_code=404, detail=str(
            "Please train the Model before Using"))


@jaseci_action(act_group=['ent_ext'], allow_remote=True)
def train(text: str, entity: List[dict]):
    """
    API for training the model
    """
    tag = []
    if text and entity:
        for ent in entity:
            if ent['entity_value'] and ent['entity_name']:
                tag.append((ent['entity_value'], ent['entity_name'],
                            ent["start_index"], ent["end_index"]))
            else:
                raise HTTPException(status_code=404, detail=str(
                    "Entity Data missing in request"))
        data = pd.DataFrame([[text, tag
                              ]], columns=['text', 'annotation'])
        # creating training data
        try:
            completed = create_data_new(data)
        except Exception as e:
            completed = create_data(data)
            print(f"Exception  : {e}")
        if completed is True:
            train_entity()
            return "Model Training is Completed"
        else:
            raise HTTPException(status_code=500, detail=str(
                "Issue encountered during train data creation"))
    else:
        raise HTTPException(status_code=404, detail=str(
            "Need Data for Text and Entity"))


@jaseci_action(act_group=['ent_ext'], allow_remote=True)
def save_model(model_path: str):
    """
    saves the model to the provided model_path
    """
    global tagger
    if tagger is not None:
        try:
            if not model_path.isalnum():
                raise HTTPException(
                    status_code=400,
                    detail='Invalid model name. Only alphanumeric chars allowed.'
                )
            if type(model_path) is str:
                model_path = Path(model_path)
            model_path.mkdir(exist_ok=True, parents=True)
            tagger.save(model_path / "final-model.pt")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        raise HTTPException(status_code=404, detail=str(
            "Please Initailize the model before saving"))


@jaseci_action(act_group=['ent_ext'], allow_remote=True)
def load_model(model_path):
    """
    loads the model from the provided model_path
    """
    global tagger
    try:
        if type(model_path) is str:
            model_path = Path(model_path)
        if (model_path / "final-model.pt").exists():
            tagger.load_state_dict(tagger.load(
                model_path / "final-model.pt").state_dict())
        tagger.to(device)
        return (f'[loaded model from] : {model_path}')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@jaseci_action(act_group=['ent_ext'], allow_remote=True)
def set_config(ner_model: str = None, model_type: str = None):
    """
    Update the configuration file with new model parameters
    ner_model types:
    1. Pre-trained LSTM / GRU : ["ner", "ner-fast","ner-large"]
    2. Huggingface model : all available models
    model_type :
    1. "TRFMODEL" : for huggingface models
    2. "LSTM" or "GRU" : RNN models
    """
    global config
    config.read(
        os.path.join(
            os.path.dirname(__file__),
            'config.cfg'
        )
    )

    if ner_model or model_type:
        config['TAGGER_MODEL']["NER_MODEL"] = ner_model
        config['TAGGER_MODEL']["MODEL_TYPE"] = model_type
    with open("config.cfg", 'w') as configfile:
        config.write(configfile)
    try:
        init_model()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return "Config setup is complete."


if __name__ == "__main__":
    from jaseci.actions.remote_actions import launch_server
    launch_server(port=8000)
