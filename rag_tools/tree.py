from anytree import Node, PreOrderIter
from anytree.search import findall_by_attr
from typing import Optional, Callable

def walk_tree(root : Node, path : str, func : Optional[Callable[[Node], None]] = None) -> Node:
    node = root
    for h in path.split('/')[1:]:
        node = findall_by_attr(node, h, maxlevel=2)[0]
        if func is not None: func(node)
    return node

def tree_to_markdown(root : Node) -> str:
    md = ""
    for n in PreOrderIter(root):
        h = n.depth-1
        header = "#" * h + " " + n.header
        text = n.text
        if h == 0 and (text == "" or text.isspace()): continue
        md += f"\n{header}\n{text}\n"
    return md

