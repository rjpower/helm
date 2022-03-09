import json
import os
import re
import gzip
import html
import random
from common.hierarchical_logger import htrack_block, hlog
from typing import List, Tuple

from common.general import ensure_file_downloaded, ensure_directory_exists
from .scenario import Scenario, Instance, Reference, TRAIN_SPLIT, VALID_SPLIT, CORRECT_TAG


class NaturalQAScenario(Scenario):
    """
    The NaturalQA dataset is from the paper:
        https://ai.google/research/pubs/pub47761

    Original repository can be found at:
        https://github.com/google-research-datasets/natural-questions

    This scenario is adapted from https://huggingface.co/datasets/natural_questions

    NaturalQA is a dataset containing 307,373 training examples with one-way
    annotations, 7,830 development examples with 5-way annotations, and 7,842 5-way annotated
    test examples. Each example consists of a context (a wikipedia document), a question, and
    one or five manually annotated long and short answers. The short answer is either a set of
    entities in the long answer, yes/no or Null.

    In this scenario, we restrict our attention to short answers. For efficiency, we
    use only the dev set---splitting in into train/validation. Additionally, we omit
    all samples in the dev set for which none of the annotators provided a short
    answer (and exclude the separate yes/no field). We only provide a single (randomly chosen)
    answer during training, and the set of all possible answers during validation.

    We consider three modes of this scenario:
    (1) closed book: No context provided
    (2) open book w/ wiki document: The entire wiki document is used as context
    (3) open book w/ long answer: Only the long answer marked by the annotators is
        provided as the context.

    The motivation to consider (3) is that the entire wiki document may not fit into
    the language model's context window.

    Concretely, we prompt models using the following format:
        (Optional) Title: <title_1>
        (Optional) Context: <context text_1>
        Question: <question_1>
        Answer: <answer_1>
        (Optional) Title: <title_2>
        (Optional) Context: <context text_2>
        Question: <question_2>
        Answer: <answer_2>
        ...
        Optional) Title: <title_k>
        (Optional) Context: <context text_k>
        Question: <question_k>
        Answer:
        Target completion:
            <answer>

    Example (mode:closed):
    Question: how many customers does edf have in the uk
    Answer: '5.7 million'

    Question: who is the largest supermarket chain in the uk
    Reference:
    ['Tesco', 'Aldi']

    Example (mode:open_longans)
    Context: A dissenting opinion (or dissent) is an opinion in a legal case in certain legal
    systems written by one or more judges expressing disagreement with the majority opinion
    of the court which gives rise to its judgment. When not necessarily
    referring to a legal decision, this can also be referred to as a minority report.[1][2]

    Question: a justice of the supreme court may write a dissenting opinion to
    Answer: 'the majority opinion of the court'

    Context: Set and filmed in New York City and based on the 1997 book of the same name by
    Candace Bushnell, the show follows the lives of a group of four women—three in their
    mid-thirties and one in her forties—who, despite their different natures and
    ever-changing sex lives, remain inseparable and confide in each other. Starring Sarah
    Jessica Parker (as Carrie Bradshaw), Kim Cattrall (as Samantha Jones), Kristin Davis
    (as Charlotte York), and Cynthia Nixon (as Miranda Hobbes), the quirky series had multiple
    continuing storylines that tackled relevant and modern social issues such as sexuality,
    safe sex, promiscuity, and femininity, while exploring the difference between friendships
    and romantic relationships. The deliberate omission of the better part of the early
    lives of the four women was the writers' way of exploring social life – from sex to
    relationships – through each of their four very different, individual perspectives.

    Question: where does sex and the city take place
    Reference:
    ['New York City']

    Example (mode:wiki)

    Title: Upstream (petroleum industry)

    Context: Upstream ( petroleum industry ) - wikipedia  Upstream ( petroleum industry )  Jump to :
    navigation, search For other uses, see Upstream (disambiguation).  The oil and gas industry
    is usually divided into three major sectors : upstream
    ( or exploration and production - E&P),...

    Question: what is upstream project in oil and gas
    Answer: 'searching for potential underground or underwater crude oil and natural gas fields,
    drilling exploratory wells, and subsequently drilling and operating the wells that recover and
    bring the crude oil or raw natural gas to the surface'

    Title: Collective Soul

    Context: Collective Soul - Wikipedia  Collective Soul  Jump to : navigation , search
    For other uses , see Collective Soul (disambiguation ) .      This article needs additional
    citations for verification .  Please help improve this article by adding citations to
    reliable sources . Unsourced material may be challenged and removed .( September 2009 )
    ( Learn how and when to remove this template message )       Collective Soul     Collective Soul
    performing at MMRBQ 2016 , Camden NJ May 21 , 2016 ...

    Question: who is the lead singer of collective soul
    Reference:
    ['Ed Roland']

    """

    name = "natural_qa"
    description = "Question answering from wikipedia."
    tags = ["question_answering"]

    def __init__(self, mode: str):
        self.context_mode = mode
        assert self.context_mode in ["openbook-wiki", "openbook-longans", "closedbook"]

    @staticmethod
    def _clean_token(token: dict):
        """Returns token in which blanks are replaced with underscores.
        Adapted from https://github.com/google-research-datasets/natural-questions/blob/master/text_utils.py
        Args:
          token: Dictionary representation of token in original NQ format.
        Returns:
          String token.
        """
        return re.sub("<([^>]*)>", "", html.unescape(re.sub(" ", "_", token["token"])))

    @staticmethod
    def _clean_text(raw_text: bytes):
        """Strips text of HTML-specific characters.
        Args:
          raw_text: Byte string
        Returns:
          String text.
        """
        text = raw_text.replace(b"\xc2\xa0", b" ").decode("utf-8")
        return re.sub("<([^>]*)>", "", html.unescape(text))

    def create_prompt(self, sample: dict, split: str) -> Tuple[str, List[str]]:

        """
        Given an example in dataset format, create the prompt and the list of
        correct references.
        """
        document = " ".join([self._clean_token(t) for t in sample["document_tokens"]])
        html_bytes = sample["document_html"].encode("utf-8")

        short_answers, long_answers = [], []
        for ans_json in sample["annotations"]:
            long_ans = ans_json["long_answer"]
            long_ans = self._clean_text(html_bytes[long_ans["start_byte"] : long_ans["end_byte"]])

            for ans in ans_json["short_answers"]:
                short_ans = html_bytes[ans["start_byte"] : ans["end_byte"]]
                short_ans = self._clean_text(short_ans)

                short_answers.append(short_ans)
                long_answers.append(long_ans)

        prompt = ""
        ans_idx = random.randint(0, len(short_answers) - 1)

        if self.context_mode != "closedbook":
            if self.context_mode == "openbook-wiki":
                context = document
                prompt += f"Title: {sample['document_title']}\n\n"
            elif self.context_mode == "openbook-longans":
                context = long_answers[ans_idx]

            prompt += f"Context: {context}\n\n"

        question = sample["question_text"].capitalize()
        if question[-1] != "?":
            question += "?"
        prompt += f"Question: {question}\n"
        prompt += "Answer:"

        if split == "train":
            answers = short_answers[ans_idx : ans_idx + 1]
        else:
            answers = list(set(short_answers))  # deduplicate

        return prompt, answers

    def get_file_instances(self, target_file: str, splits: dict) -> List[Instance]:
        """
        Helper for generating instances for the given splits.
        Args:
            target_file (str): Data file.
            split (dict): Which splits to partition the data into.

        Returns:
            List[Instance]: Instances from file partitioned uniformly across splits.
        """
        instances: List[Instance] = []

        all_samples: List[dict] = []

        with htrack_block(f"Reading {target_file}"):
            with gzip.open(target_file, "rb") as fp:
                for line in fp:
                    raw = json.loads(line)
                    # Only keep dataset samples with at least one short answer
                    if any([len(anno["short_answers"]) for anno in raw["annotations"]]):
                        all_samples.append(raw)
            hlog(f"{len(all_samples)} examples")

        for si, sample in enumerate(all_samples):

            # Assign even/odd samples to the train and val splits respectively
            split = "train" if si % 2 == 0 else "val"

            prompt, answers = self.create_prompt(sample, split)

            instance = Instance(
                input=prompt,
                references=[Reference(output=ans, tags=[CORRECT_TAG]) for ans in answers],
                split=splits[split],
            )
            instances.append(instance)

        return instances

    def get_instances(self) -> List[Instance]:
        data_path = os.path.join(self.output_path, "data")
        ensure_directory_exists(data_path)
        random.seed(0)

        base_url: str = "https://storage.googleapis.com/natural_questions/v1.0/dev"
        file_list: List[str] = ["nq-dev-%02d.jsonl.gz" % i for i in range(5)]

        instances: List[Instance] = []
        splits = {"train": TRAIN_SPLIT, "val": VALID_SPLIT}
        for file in file_list:
            source_url: str = f"{base_url}/{file}"
            target_path: str = os.path.join(data_path, f"{file}")
            ensure_file_downloaded(source_url=source_url, target_path=target_path)

            instances.extend(self.get_file_instances(target_path, splits=splits))

        return instances
