from dataclasses import dataclass, field
from enum import Enum
import functools
from pathlib import Path
import logging
import re
import shutil
import subprocess
import sys
from typing import Optional, List, Dict, Union, Tuple
import os
import warnings

import dacite
import git
import github
import jinja2
import networkx as nx
from markupsafe import Markup
import requests
import yaml
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

import make_old_html
import port_status_yaml
from htmlify_comment import htmlify_comment, htmlify_text
from get_mathlib4_history import get_mathlib4_history, FileHistoryEntry

github_token = os.environ.get("GITHUB_TOKEN")


@functools.cache
def mathlib4repo():
    return github.Github(github_token).get_repo("leanprover-community/mathlib4")


def is_uninteresting_commit(c: git.Commit):
    if c.summary.startswith('chore(*): add mathlib4 synchronization comments'):
        return True
    elif c.hexsha == '448144f7ae193a8990cb7473c9e9a01990f64ac7':
        return True
    else:
        return False


def commits_and_diffs_between(base_commit: git.Commit, head_commit: git.Commit, fname: str
        ) -> List[Tuple[git.Commit, Optional[git.Diff]]]:
    """ Get commits between base_commit and head_commit, with diffs when `fname` is touched """
    commits = []
    last = base_commit
    for c in base_commit.repo.iter_commits(f'{base_commit.hexsha}..{head_commit.hexsha}', fname, reverse=True):
        # record all the intermediate commits
        for c_between in base_commit.repo.iter_commits(f'{last.hexsha}..{c.hexsha}^', reverse=True):
            commits.insert(0, (c_between, None))
        if is_uninteresting_commit(c):
            commits.insert(0, (c, None))
        else:
            diffs = last.diff(c, paths=fname, create_patch=True)
            try:
                diff, = diffs
            except ValueError:
                diff = None
            commits.insert(0, (c, diff))
        last = c
    for c_between in base_commit.repo.iter_commits(f'{last.hexsha}..{head_commit.hexsha}^', reverse=True):
        commits.insert(0, (c_between, None))
    return commits

@functools.cache
def github_labels(pr):
    try:
        pull_request = mathlib4repo().get_pull(pr)
        raw_labels = list(pull_request.get_labels())
    except github.RateLimitExceededException:
        if 'GITPOD_HOST' in os.environ:
            warnings.warn(
                'Unable to fetch PR labels; set `GITHUB_TOKEN` to increase the rate limit')
            return []
        raise
    def text_color_of_color(color):
        r, g, b = map(lambda i: int(color[i:i + 2], 16), (0, 2, 4))
        perceived_lightness = (
            (r * 0.2126) + (g * 0.7152) + (b * 0.0722)) / 255
        lightness_threshold = 0.453
        return 'black' if perceived_lightness > lightness_threshold else 'white'

    labels = [{'name': label.name,
               'color': label.color,
               'text_color': text_color_of_color(label.color)}
              for label in raw_labels]
    return labels


def parse_imports(root_path):
    import_re = re.compile(r"^import ([^ ]*)")

    def mk_label(path: Path) -> str:
        return '.'.join(path.relative_to(root_path).with_suffix('').parts)

    graph = nx.DiGraph()

    for path in root_path.glob('**/*.lean'):
        if path.parts[1] in ['tactic', 'meta']:
            continue
        graph.add_node(mk_label(path))

    for path in root_path.glob('**/*.lean'):
        if path.parts[1] in ['tactic', 'meta']:
            continue
        label = mk_label(path)
        for line in path.read_text().split('\n'):
            m = import_re.match(line)
            if m:
                imported = m.group(1)
                if imported.startswith('tactic.') or imported.startswith('meta.'):
                    continue
                if imported not in graph.nodes:
                    if imported + '.default' in graph.nodes:
                        imported = imported + '.default'
                    else:
                        imported = 'lean_core.' + imported
                graph.add_edge(imported, label)
    return graph

class PortState(Enum):
    UNPORTED = 'UNPORTED'
    IN_PROGRESS = 'IN_PROGRESS'
    PORTED = 'PORTED'

@dataclass
class ForwardPortInfo:
    base_commit: git.Commit
    all_unported_commits: List[Tuple[git.Commit, git.Diff]]
    all_ported_commits: List[Tuple[git.Commit, git.Diff]]
    diff_lines: List[str]

    @property
    def ported_commits(self):
        return [(c, d) for c, d in self.all_ported_commits if d is not None]

    @property
    def unported_commits(self):
        return [(c, d) for c, d in self.all_unported_commits if d is not None]

    @property
    def diff(self) -> str:
        return "\n".join(self.diff_lines)

    @property
    def diff_stat(self) -> Tuple[int, int]:
        return (
            sum(l.startswith('+') for l in self.diff_lines),
            sum(l.startswith('-') for l in self.diff_lines)
        )



@dataclass
class Mathlib3FileData:
    mathlib3_import: List[str]
    status: port_status_yaml.PortStatusEntry
    lines: Optional[int]
    labels: Optional[List[dict[str, str]]]
    dependents: Optional[List['Mathlib3FileData']] = None
    dependencies: Optional[List['Mathlib3FileData']] = None
    forward_port: Optional[ForwardPortInfo] = None
    mathlib4_history: List[FileHistoryEntry] = field(default_factory=list)

    @functools.cached_property
    def state(self):
        if self.status.ported:
            return PortState.PORTED
        elif self.status.mathlib4_pr and self.status.source:
            # PR is meaningless without the hash, as it might be an ad-hoc port
            return PortState.IN_PROGRESS
        else:
            return PortState.UNPORTED
    @functools.cached_property
    def dep_counts(self):
        if self.dependencies is not None:
            return tuple(
                len([x for x in self.dependencies if x.state == s])
                for s in PortState)
        else:
            return None

    @functools.cached_property
    def dep_counts_sort_key(self) -> int:
        IN_PROGRESS_EQUIV_UNPORTED = 5
        if self.dep_counts is None:
            return sys.maxsize
        u, i, p = self.dep_counts
        return u*10000*IN_PROGRESS_EQUIV_UNPORTED+i*10000

    @property
    def dep_graph_data(self) -> Tuple[List[Tuple[str, str]], Dict[str, PortState]]:
        unported_deps = [self] + [d for d in self.dependencies if d.state !=  PortState.PORTED]
        unported_deps_names = {'.'.join(d.mathlib3_import) for d in unported_deps}
        g = graph.subgraph(unported_deps_names)
        node_data = {n: d["data"].state.value for (n, d) in g.nodes().items() if "data" in d}
        return list(g.edges()), node_data

@functools.cache
def get_repo_by_github_name(url: str) -> git.Repo:
    if url == 'leanprover-community/mathlib':
        return git.Repo(mathlib_dir)
    elif url == 'leanprover-community/mathlib4':
        return git.Repo(mathlib4_dir)
    else:
        raise KeyError(url)


@functools.cache
def get_github_name(repo: git.Repo):
    url = repo.remotes[0].url
    if url.startswith('https://github.com/'):
        return url.removeprefix('https://github.com/')
    elif url.startswith('git@github.com:'):
        return url.removeprefix('git@github.com:')
    else:
        raise RuntimeError(f"Unrecognized repo {url}")



@functools.cache
def commit_exists(src: port_status_yaml.PortStatusEntry.Source) -> bool:
    try:
        repo = get_repo_by_github_name(src.repo)
    except KeyError:
        return True
    else:
        try:
            repo.commit(src.commit)
        except ValueError:
            return False
        else:
            return True

def link_sha(sha: Union[port_status_yaml.PortStatusEntry.Source, git.Commit]) -> Markup:
    if isinstance(sha, git.Commit):
        url = get_github_name(sha.repo)
        sha = port_status_yaml.PortStatusEntry.Source(repo=url, commit=sha.hexsha)
        valid = True
    else:
        valid = commit_exists(sha)

    if isinstance(sha, port_status_yaml.PortStatusEntry.Source):
        return Markup(
            '<a href="https://github.com/{repo}/commit/{sha}"' +
                (' class="font-monospace text-danger" title="commit does not seem to exist!"' if not valid else
                 ' class="font-monospace"') +
                '>{short_sha}</a>'
        ).format(repo=sha.repo, sha=sha.commit, short_sha=sha.commit[:8],
            extra=' class="text-danger" title="commit does not seem to exist!"' if not valid else '')
    else:
        return Markup('<span title="Unknown" class="text-danger">???</span>')

port_status = port_status_yaml.load()

build_dir = Path('build')
build_dir.mkdir(parents=True, exist_ok=True)

template_loader = jinja2.FileSystemLoader(searchpath="templates/")
template_env = jinja2.Environment(loader=template_loader)
template_env.filters['htmlify_comment'] = htmlify_comment
template_env.filters['htmlify_text'] = htmlify_text
template_env.filters['link_sha'] = link_sha
template_env.filters['set'] = set
template_env.globals['site_url'] = os.environ.get('SITE_URL', '')
template_env.globals['PortState'] = PortState
template_env.globals['nx'] = nx

mathlib_dir = build_dir / 'repos' / 'mathlib'
mathlib4_dir = build_dir / 'repos' / 'mathlib4'

graph = parse_imports(mathlib_dir / 'src')
graph = nx.transitive_reduction(graph)

(build_dir / 'html').mkdir(parents=True, exist_ok=True)

shutil.copytree(Path('static'), build_dir / 'html', dirs_exist_ok=True)

@functools.cache
def get_data():
    data = {}
    max_len = max((len(i) for i in port_status), default=0)
    with tqdm(port_status.items(), desc='getting status information') as pbar:
        for f_import, f_status in pbar:
            pbar.set_postfix_str(f_import.ljust(max_len), refresh=False)
            path = mathlib_dir / 'src' / Path(*f_import.split('.')).with_suffix('.lean')
            try:
                with path.open('r') as f_src:
                    lines = len(f_src.readlines())
            except IOError:
                lines = None
            data[f_import] = Mathlib3FileData(
                mathlib3_import=f_import.split('.'),
                status=f_status,
                lines=lines,
                labels=github_labels(f_status.mathlib4_pr) if ((not f_status.ported) and
                                                                f_status.mathlib4_pr) else []
            )

    with tqdm(data.items(), desc='building import graph') as pbar:
        for f_import, f_data in pbar:
            pbar.set_postfix_str(f_import.ljust(max_len), refresh=False)
            if f_import in graph:
                f_data.dependents = [
                    data[k] for k in nx.descendants(graph, f_import) if k in data
                ]
                f_data.dependencies = [
                    data[k] for k in nx.ancestors(graph, f_import) if k in data
                ]
                graph.nodes[f_import]["data"] = f_data


    history = get_mathlib4_history(git.Repo(mathlib4_dir))
    for f_import, f_data in data.items():
        f_data.mathlib4_history = history.get(f_import, [])

    return data

def make_index(env, html_root):
    data = get_data()
    ported = {}
    in_progress = {}
    unported = {}
    groups = {
        PortState.PORTED: ported,
        PortState.IN_PROGRESS: in_progress,
        PortState.UNPORTED: unported,
    }
    for f_import, f_data in data.items():
        groups[f_data.state][f_import] = f_data

    with (build_dir / 'html' / 'index.html').open('w') as index_f:
        index_f.write(
            env.get_template('index.j2').render(
                all=data.values(),
                ported=ported, unported=unported, in_progress=in_progress))

def make_out_of_sync(env, html_root, mathlib_dir):
    # Not using re.compile as this is passed to git which uses a different regex dialect:
    # https://www.sjoerdlangkemper.nl/2021/08/13/how-does-git-diff-ignore-matching-lines-work/
    comment_git_re = r'\`(' + r'|'.join([
        re.escape("> THIS FILE IS SYNCHRONIZED WITH MATHLIB4."),
        re.escape("> https://github.com/leanprover-community/mathlib4/pull/") + r"[0-9]*",
        re.escape("> Any changes to this file require a corresponding PR to mathlib4."),
        r"",
    ]) + r")" + "\n"

    mathlib_repo = git.Repo(mathlib_dir)
    data = get_data()

    max_len = max((len(i) for i in port_status), default=0)
    with tqdm(data.items(), desc='generating mathlib3 diffs') as pbar:
        for f_import, f_status in pbar:
            pbar.set_postfix_str(f_import.ljust(max_len), refresh=False)
            if not f_status.status.source or f_status.status.source.repo != 'leanprover-community/mathlib':
                continue
            fname = "src" + os.sep + f_import.replace('.', os.sep) + ".lean"
            try:
                sync_commit = mathlib_repo.commit(f_status.status.source.commit)
            except Exception:
                sync_commit = None

            base_commit = None
            if f_status.mathlib4_history:
                # find the first sha that's actually real
                for h in reversed(f_status.mathlib4_history):
                    try:
                        base_commit = mathlib_repo.commit(h.source.commit)
                    except Exception:
                        continue
                    else:
                        break

            if not base_commit and sync_commit:
                # base commit is expected to be missing unless the file is in mathlib4 master
                if f_status.state == PortState.PORTED:
                    logging.warning(f"no base commit for: {f_import}")
                base_commit = sync_commit
            elif not sync_commit and base_commit:
                if f_status.state != PortState.UNPORTED:
                    logging.warning(f"no sync commit for: {f_import}")
                sync_commit = base_commit
            elif not sync_commit and not base_commit:
                if f_status.state != PortState.UNPORTED:
                    logging.warning(f"no commits at all for: {f_import} {f_status.state}")
                continue

            git_command = ['git',
                'diff', '--exit-code',
                f'--ignore-matching-lines={comment_git_re}',
                sync_commit.hexsha + "..HEAD", "--", fname]
            result = subprocess.run(git_command, cwd=mathlib_dir, capture_output=True, encoding='utf8')

            ported_commits = commits_and_diffs_between(base_commit, sync_commit, fname)
            unported_commits = commits_and_diffs_between(sync_commit, mathlib_repo.head.commit, fname)

            if result.returncode == 1:
                data[f_import].forward_port = ForwardPortInfo(base_commit, unported_commits, ported_commits, result.stdout.splitlines()[4:])
            else:
                data[f_import].forward_port = ForwardPortInfo(base_commit, unported_commits, ported_commits, "")

    file_template = env.get_template('file.j2')
    with tqdm(port_status.items(), desc="generating file pages") as pbar:
        for f_import, f_status in pbar:
            pbar.set_postfix_str(f_import.ljust(max_len), refresh=False)
            path = (html_root / 'file' / Path(*f_import.split('.')).with_suffix('.html'))
            path.parent.mkdir(exist_ok=True, parents=True)
            with path.open('w') as file_f:
                if f_status.mathlib4_file is None:
                    mathlib4_import = None
                else:
                    mathlib4_import = Path(f_status.mathlib4_file).with_suffix('').parts
                for chunk in file_template.generate(
                    mathlib3_import=f_import.split('.'),
                    mathlib4_import=mathlib4_import,
                    data=get_data().get(f_import),
                    graph=graph,
                ):
                    file_f.write(chunk)

    with (html_root / 'out-of-sync.html').open('w') as index_f:
        index_f.write(env.get_template('out-of-sync.j2').render(
            head_sha=mathlib_repo.head.object,
            data=get_data(),
        ))

if __name__ == "__main__":
    with logging_redirect_tqdm():
        make_index(template_env, build_dir / 'html')
        make_out_of_sync(template_env, build_dir / 'html', mathlib_dir)
        make_old_html.make_old(template_env, build_dir / 'html', mathlib_dir)
