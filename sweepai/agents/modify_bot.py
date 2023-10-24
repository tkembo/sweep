import copy
import re
import uuid

from loguru import logger

from sweepai.agents.complete_code import ExtractLeftoverComments
from sweepai.agents.graph_child import extract_python_span
from sweepai.agents.prune_modify_snippets import PruneModifySnippets
from sweepai.agents.validate_code import ChangeValidation, ChangeValidator
from sweepai.config.server import DEBUG
from sweepai.core.chat import ChatGPT
from sweepai.core.entities import FileChangeRequest, Message, UnneededEditError
from sweepai.core.prompts import dont_use_chunking_message, use_chunking_message
from sweepai.core.update_prompts import (
    update_snippets_prompt,
    update_snippets_prompt_test,
    update_snippets_system_prompt,
    update_snippets_system_prompt_python,
)
from sweepai.utils.code_tree import CodeTree
from sweepai.utils.diff import generate_diff, sliding_window_replacement
from sweepai.utils.function_call_utils import find_function_calls
from sweepai.utils.search_and_replace import Match, find_best_match, split_ellipses

fetch_snippets_system_prompt = """You are a masterful engineer. Your job is to extract the original lines from the code that should be modified. The snippets will be modified after extraction so make sure we can match the snippets to the original code.

Extract the smallest spans that let you handle the request by adding blocks of snippet_to_modify containing the code blocks you want to modify. Use this for implementing or changing functionality.

Then, write search terms to extract that we need to modify from the code. The system will then modify all of the lines containing the patterns. Use this to make many small changes, such as updating all function calls after changing the signature.

# Format
<analysis_and_identification file="file_path">
Identify all changes that need to be made to the file.
In a list, identify all code sections that should receive these changes and all locations code should be added. These snippets will go into the snippets_to_modify block. Pick many small snippets and locations to add code instead of a single large one.
Then identify any patterns of code that should be modified, like all function calls of a particular function. These patterns will go into the patterns block.
</analysis_and_identification>

<snippets_to_modify>
<snippet_to_modify reason="justification for modifying this snippet">
```
first few lines from the first original snippet
...
last few lines from the first original snippet (the code)
```
</snippet_to_modify>
<snippet_to_modify reason="justification for modifying this snippet">
```
first few lines from the second original snippet
...
last few lines from the second original snippet (the code)
```
</snippet_to_modify>
...
</snippets_to_modify>

<extraction_terms>
first term from the code
second term from the code
...
</extraction_terms>"""

fetch_snippets_prompt = """
# Code
File path: {file_path}
<old_code>
```
{code}
```
</old_code>
{changes_made}
# Request
{request}

# Instructions
{chunking_message}

# Format
<analysis_and_identification file="file_path">
Identify all changes that need to be made to the file.
In a list, identify all code sections that should receive these changes and all locations code should be added. These snippets will go into the snippets_to_modify block. Pick many small snippets and locations to add code instead of a single large one.
Then identify any patterns of code that should be modified, like all function calls of a particular function. These patterns will go into the patterns block.
</analysis_and_identification>

<snippets_to_modify>
<snippet_to_modify reason="justification for modifying this snippet">
```
first few lines from the first original snippet
...
last few lines from the first original snippet (the code)
```
</snippet_to_modify>
<snippet_to_modify reason="justification for modifying this snippet">
```
first few lines from the second original snippet
...
last few lines from the second original snippet (the code)
```
</snippet_to_modify>
...
</snippets_to_modify>

<extraction_terms>
first term from the code
second term from the code
...
</extraction_terms>"""

plan_snippets_system_prompt = """\
You are a brilliant and meticulous engineer assigned to plan code changes to complete the user's request.

You will plan code changes to solve the user's problems. You have the utmost care for the plans you write, so you do not make mistakes and you fully implement every function and class. Take into account the current repository's language, code style, and dependencies.

You will be given the old_file and potentially relevant snippets to edit. You do not necessarily have to edit all the snippets.

Respond in the following format:

<snippets_and_plan_analysis file="file_path">
Describe what should be changed to the snippets from the old_file to complete the request.
Then, for each snippet, describe in a list the changes needed, with references to the lines that should be changed and what to change it to.
Maximize information density.
</snippets_and_plan_analysis>
"""

plan_snippets_prompt = """# Code
File path: {file_path}
<old_code>
```
{code}
```
</old_code>
{changes_made}
# Request
{request}

<snippets_to_update>
{snippets}
</snippets_to_update>

# Instructions
Rewrite each of the {n} snippets above according to the request.
* Do not delete whitespace or comments.
* Write minimal diff hunks to make changes to the snippets. Only write diffs for the lines that should be changed.
* Write multiple smalle changes instead of a single large change.
* To add code before and after the snippet, be sure to copy the original snippet.

Respond in the following format:

<snippets_and_plan_analysis file="file_path">
Describe what should be changed to the snippets from the old_file to complete the request.
Then, for each snippet, describe in a list the changes needed, with references to the lines that should be changed and what to change it to.
Maximize information density.
</snippets_and_plan_analysis>
"""


def strip_backticks(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s[s.find("\n") :]
    if s.endswith("```"):
        s = s[: s.rfind("\n")]
    s = s.strip("\n")
    if s == '""':
        return ""
    return s


class ModifyBot:
    def __init__(
        self,
        additional_messages: list[Message] = [],
        chat_logger=None,
        parent_bot: ChatGPT = None,
        old_file_contents: str = "",
        current_file_diff: str = "",
        **kwargs,
    ):
        self.fetch_snippets_bot: ChatGPT = ChatGPT.from_system_message_string(
            fetch_snippets_system_prompt, chat_logger=chat_logger, **kwargs
        )
        self.fetch_snippets_bot.messages.extend(additional_messages)
        self.plan_bot: ChatGPT = ChatGPT.from_system_message_string(
            plan_snippets_system_prompt, chat_logger=chat_logger, **kwargs
        )
        self.plan_bot.messages.extend(additional_messages)
        self.update_snippets_bot: ChatGPT = ChatGPT.from_system_message_string(
            update_snippets_system_prompt, chat_logger=chat_logger, **kwargs
        )
        self.update_snippets_bot.messages.extend(additional_messages)
        self.parent_bot = parent_bot

        self.extract_leftover_comments_bot: ExtractLeftoverComments = (
            ExtractLeftoverComments(chat_logger=chat_logger, **kwargs)
        )
        self.extract_leftover_comments_bot.messages.extend(additional_messages)
        self.prune_modify_snippets_bot: PruneModifySnippets = PruneModifySnippets(
            chat_logger=chat_logger, **kwargs
        )
        self.prune_modify_snippets_bot.messages.extend(additional_messages)
        self.chat_logger = chat_logger
        self.additional_messages = additional_messages
        self.old_file_contents = old_file_contents
        self.current_file_diff = current_file_diff
        self.additional_diffs = ""

    def get_diffs_message(self, file_contents: str):
        if self.current_file_diff == "" and self.old_file_contents == file_contents:
            return self.additional_diffs
        elif self.current_file_diff == "":
            diff = generate_diff(self.old_file_contents, file_contents)
        elif self.old_file_contents == file_contents:
            diff = self.current_file_diff
        else:
            diff = (
                self.old_file_contents
                + "\n...\n"
                + generate_diff(self.old_file_contents, file_contents)
            )
        diff += self.additional_diffs
        return f"\n# Changes Made\nHere are changes we already made to this file:\n<diff>\n{diff}\n</diff>\n"

    def try_update_file(
        self,
        file_path: str,
        file_contents: str,
        file_change_request: FileChangeRequest,
        chunking: bool = False,
    ):
        (
            snippet_queries,
            extraction_terms,
            analysis_and_identification,
        ) = self.get_snippets_to_modify(
            file_path=file_path,
            file_contents=file_contents,
            file_change_request=file_change_request,
            chunking=chunking,
        )

        new_file, leftover_comments, change_validation = self.update_file(
            file_path=file_path,
            file_contents=file_contents,
            file_change_request=file_change_request,
            snippet_queries=snippet_queries,
            extraction_terms=extraction_terms,
            chunking=chunking,
            analysis_and_identification=analysis_and_identification,
        )
        for _ in range(3):
            if leftover_comments and not DEBUG:
                joined_comments = "\n".join(leftover_comments)
                new_file_change_request = copy.deepcopy(file_change_request)
                new_file_change_request.new_content = new_file
                new_file_change_request.id_ = str(uuid.uuid4())
                new_file_change_request.instructions = f"Address all of the unfinished code changes here: \n{joined_comments}"
                self.fetch_snippets_bot.messages = self.fetch_snippets_bot.messages[:-2]
                self.prune_modify_snippets_bot.messages = (
                    self.prune_modify_snippets_bot.messages[:-2]
                )
                (
                    snippet_queries,
                    extraction_terms,
                    analysis_and_identification,
                ) = self.get_snippets_to_modify(
                    file_path=file_path,
                    file_contents=new_file,
                    file_change_request=new_file_change_request,
                    chunking=chunking,
                )
                self.update_snippets_bot.messages = self.update_snippets_bot.messages[
                    :-2
                ]
                new_file, leftover_comments, change_validation = self.update_file(
                    file_path=file_path,
                    file_contents=new_file,
                    file_change_request=new_file_change_request,
                    snippet_queries=snippet_queries,
                    extraction_terms=extraction_terms,
                    chunking=chunking,
                    analysis_and_identification=analysis_and_identification,
                )
            if change_validation.additional_changes_required:
                file_change_request.new_content = new_file
                file_change_request.instructions = change_validation.additional_changes
                self.fetch_snippets_bot.messages = self.fetch_snippets_bot.messages[:-2]
                self.prune_modify_snippets_bot.messages = (
                    self.prune_modify_snippets_bot.messages[:-2]
                )
                # TODO: delete messages in the bots themselves
                (
                    snippet_queries,
                    extraction_terms,
                    analysis_and_identification,
                ) = self.get_snippets_to_modify(
                    file_path=file_path,
                    file_contents=file_contents,
                    file_change_request=file_change_request,
                    chunking=chunking,
                )
                self.update_snippets_bot.messages = self.update_snippets_bot.messages[
                    :-2
                ]
                new_file, leftover_comments, change_validation = self.update_file(
                    file_path=file_path,
                    file_contents=file_contents,
                    file_change_request=file_change_request,
                    snippet_queries=snippet_queries,
                    extraction_terms=extraction_terms,
                    chunking=chunking,
                    analysis_and_identification=analysis_and_identification,
                )
        return new_file

    def get_snippets_to_modify(
        self,
        file_path: str,
        file_contents: str,
        file_change_request: FileChangeRequest,
        chunking: bool = False,
    ):
        fetch_snippets_response = self.fetch_snippets_bot.chat(
            fetch_snippets_prompt.format(
                code=extract_python_span(
                    file_contents, [file_change_request.entity]
                ).content
                if file_change_request.entity
                else file_contents,
                changes_made=self.get_diffs_message(file_contents),
                file_path=file_path,
                request=file_change_request.instructions,
                chunking_message=use_chunking_message
                if chunking
                else dont_use_chunking_message,
            )
        )
        analysis_and_identification_pattern = r"<analysis_and_identification.*?>\n(?P<code>.*)\n</analysis_and_identification>"
        analysis_and_identification_match = re.search(
            analysis_and_identification_pattern, fetch_snippets_response, re.DOTALL
        )
        analysis_and_identifications_str = (
            analysis_and_identification_match.group("code").strip()
            if analysis_and_identification_match
            else ""
        )

        extraction_terms = []
        extraction_term_pattern = (
            r"<extraction_terms.*?>\n(?P<extraction_term>.*?)\n</extraction_terms>"
        )
        for extraction_term in re.findall(
            extraction_term_pattern, fetch_snippets_response, re.DOTALL
        ):
            for term in extraction_term.split("\n"):
                term = term.strip()
                if term:
                    extraction_terms.append(term)
        snippet_queries = []
        snippets_query_pattern = (
            r"<snippet_to_modify.*?>\n(?P<code>.*?)\n</snippet_to_modify>"
        )
        for code in re.findall(
            snippets_query_pattern, fetch_snippets_response, re.DOTALL
        ):
            snippet_queries.append(strip_backticks(code))

        if len(snippet_queries) == 0:
            raise UnneededEditError("No snippets found in file")
        return snippet_queries, extraction_terms, analysis_and_identifications_str

    def update_file(
        self,
        file_path: str,
        file_contents: str,
        file_change_request: FileChangeRequest,
        snippet_queries: list[str],
        extraction_terms: list[str],
        chunking: bool = False,
        analysis_and_identification: str = "",
    ):
        is_python_file = file_path.strip().endswith(".py")

        best_matches = []
        for query in snippet_queries:
            if query.count("...") > 2:
                for section in split_ellipses(query):
                    match_ = find_best_match(section, file_contents)
                    if match_.score > 50:
                        best_matches.append(match_)
            else:
                match_ = find_best_match(query, file_contents)
                if match_.score > 50:
                    best_matches.append(match_)

        code_tree = CodeTree.from_code(file_contents) if is_python_file else None
        for i, line in enumerate(file_contents.split("\n")):
            for keyword in extraction_terms:
                if keyword in line:
                    try:
                        if is_python_file:
                            start_line, end_line = code_tree.get_lines_surrounding(i)
                        else:
                            start_line, end_line = i, i
                    except Exception as e:
                        logger.error(e)
                        start_line, end_line = i, i
                    best_matches.append(
                        Match(
                            start=start_line,
                            end=end_line + 1,
                            score=100,
                        )
                    )

        # Get all line matches where the keyword is either mentioned or used as a function call
        for keyword in extraction_terms:
            keyword = keyword.rstrip("()")
            for start, end in find_function_calls(keyword, file_contents):
                best_matches.append(
                    Match(
                        start=start,
                        end=end + 1,
                        score=100,
                    )
                )
        # get first 10 lines for imports
        IMPORT_LINES = 10
        best_matches.append(
            Match(
                start=0,
                end=min(IMPORT_LINES, len(file_contents.split("\n"))),
                score=100,
            )
        )

        if len(best_matches) == 0:
            raise UnneededEditError("No matches found in file")

        # Todo: check multiple files for matches using PR changed files

        best_matches.sort(key=lambda x: x.start + x.end * 0.00001)

        def fuse_matches(a: Match, b: Match) -> Match:
            return Match(
                start=min(a.start, b.start),
                end=max(a.end, b.end),
                score=min(a.score, b.score),
            )

        current_match = best_matches[0]
        deduped_matches: list[Match] = []

        # Fuse & dedup
        FUSE_OFFSET = 5
        for next_match_ in best_matches[1:]:
            if (
                current_match.end > next_match_.start
                or abs(current_match.end - next_match_.start) <= FUSE_OFFSET
            ):
                current_match = fuse_matches(current_match, next_match_)
            else:
                deduped_matches.append(current_match)
                current_match = next_match_
        deduped_matches.append(current_match)
        if is_python_file:
            new_deduped_matches = []
            for match_ in deduped_matches:
                start_line = code_tree.get_lines_surrounding(match_.start)[0]
                end_line = code_tree.get_lines_surrounding(match_.end)[1]
                new_deduped_matches.append(
                    Match(
                        start=start_line,
                        end=end_line + 1,
                        score=match_.score,
                    )
                )
            deduped_matches = new_deduped_matches

        selected_snippets = []
        file_contents_lines = file_contents.split("\n")
        for match_ in deduped_matches:
            current_contents = "\n".join(file_contents_lines[match_.start : match_.end])
            selected_snippets.append(current_contents)

        update_snippets_code = file_contents
        if file_change_request.entity:
            update_snippets_code = extract_python_span(
                file_contents, [file_change_request.entity]
            ).content

        if len(selected_snippets) > 1:
            indices_to_keep = self.prune_modify_snippets_bot.prune_modify_snippets(
                snippets="\n\n".join(
                    [
                        f'<snippet index="{i}">\n{snippet}\n</snippet>'
                        for i, snippet in enumerate(selected_snippets)
                    ]
                ),
                file_path=file_path,
                changes_made=self.get_diffs_message(file_contents),
                old_code=update_snippets_code,
                request=file_change_request.instructions
                + "\n"
                + analysis_and_identification,
            )
        else:
            indices_to_keep = [0]

        if len(indices_to_keep) == 0:
            raise UnneededEditError("No snippets selected")

        pruned_snippets = []
        for idx, snippet in enumerate(selected_snippets):
            if idx in indices_to_keep:
                pruned_snippets.append(snippet)
        selected_snippets = pruned_snippets

        if is_python_file:
            self.update_snippets_bot.messages[
                0
            ].content = update_snippets_system_prompt_python

        analysis_response = self.plan_bot.chat(
            plan_snippets_prompt.format(
                code=update_snippets_code,
                file_path=file_path,
                snippets="\n\n".join(
                    [
                        f'<snippet index="{i}">\n{snippet}\n</snippet>'
                        for i, snippet in enumerate(selected_snippets)
                    ]
                ),
                request=file_change_request.instructions
                + "\n"
                + analysis_and_identification,
                n=len(selected_snippets),
                changes_made=self.get_diffs_message(file_contents),
            )
        )

        if file_change_request.failed_sandbox_test:
            update_prompt = update_snippets_prompt_test
        else:
            update_prompt = update_snippets_prompt
        update_snippets_response = self.update_snippets_bot.chat(
            update_prompt.format(
                code=update_snippets_code,
                file_path=file_path,
                snippets="\n\n".join(
                    [
                        f'<snippet index="{i}">\n{snippet}\n</snippet>'
                        for i, snippet in enumerate(selected_snippets)
                    ]
                ),
                analysis=analysis_response,
                request=file_change_request.instructions
                + "\n"
                + analysis_and_identification,
                n=len(selected_snippets),
                changes_made=self.get_diffs_message(file_contents),
            )
        )
        updated_snippets: dict[int, str] = {}
        updated_pattern = r"<<<<<<<\s+ORIGINAL\s+\(index=(?P<index>\d+)\)(?P<original_code>.*?)=======(?P<updated_code>.*?)>>>>>>>\s+UPDATED"

        if (
            len(list(re.finditer(updated_pattern, update_snippets_response, re.DOTALL)))
            == 0
        ):
            raise UnneededEditError("No snippets edited")

        for match_ in re.finditer(updated_pattern, update_snippets_response, re.DOTALL):
            index = int(match_.group("index"))
            original_code = match_.group("original_code").strip("\n")
            updated_code = match_.group("updated_code").strip("\n")

            current_contents = selected_snippets[index]
            if index not in updated_snippets:
                updated_snippets[index] = current_contents
            else:
                current_contents = updated_snippets[index]
            updated_snippets[index] = "\n".join(
                sliding_window_replacement(
                    original=current_contents.splitlines(),
                    search=original_code.splitlines(),
                    replace=updated_code.splitlines(),
                )[0]
            )

        change_validator = ChangeValidator.create(
            file_contents,
            file_change_request,
            selected_snippets,
            updated_snippets,
            chat_logger=self.chat_logger,
            additional_messages=self.additional_messages,
        )
        change_validation = ChangeValidation(
            analysis="",
            additional_changes="",
            additional_changes_required_raw="no",
            diffs_to_revert_raw="",
        )
        result = change_validator.apply_validated_changes(change_validation)

        new_code = []
        for idx, search in enumerate(selected_snippets):
            if idx not in updated_snippets:
                continue
            if selected_snippets.index(search) not in change_validator.updated_snippets:
                continue
            replace = change_validator.updated_snippets[selected_snippets.index(search)]
            new_code.append(replace)

        ending_newlines = len(file_contents) - len(file_contents.rstrip("\n"))
        result = result.rstrip("\n") + "\n" * ending_newlines

        new_code = "\n".join(new_code)
        leftover_comments = (
            (
                self.extract_leftover_comments_bot.extract_leftover_comments(
                    new_code,
                    file_path,
                    file_change_request.instructions,
                )
            )
            if not DEBUG
            else []
        )

        return result, leftover_comments, change_validation


if __name__ == "__main__":
    response = """
```python
```"""
    stripped = strip_backticks(response)
    print(stripped)