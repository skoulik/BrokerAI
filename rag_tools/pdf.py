import pymupdf
import math
import copy
import string
import random
import os
from typing import Optional, List, Tuple, Dict
from anytree import Node, PreOrderIter, util

def _contain(trs : List[pymupdf.Rect], r : pymupdf.Rect) -> bool:
    for tr in trs:
        if(tr.contains(r)):
            return True
    return False

def _sanitize_text(text : str) -> str:
    return (text
            .replace(u"\uF0B7",  "-")  #bullet in some fonts, private
            .replace(u"\u2022",  "-")  #bullet
            .replace(u"\u25CF",  "-")  #circle
            .replace(u"\u2013",  "-")  #dash
            .replace(u"\u00B7",  "-")  #middle dot
            .replace(u"\u0007",   "")  #alert
            .replace(u"\u0008",   "")  #backspace (wtf?)
            .replace(u"\u0009", "  ")  #tab
            )


def _detect_columns(
        clip                   : pymupdf.Rect,
        text_rects             : List[pymupdf.Rect],
        exclude_rects          : Optional[List[pymupdf.Rect]] = [],
        min_col_width          : Optional[float] = None,
        max_columns            : Optional[int] = 10,
        block_crossing_penalty : Optional[float] = 1.0
    ) -> List[Tuple[float, float]]:

    if min_col_width is None: min_col_width = clip.width / max_columns

    rects = list(filter(lambda r: not _contain(exclude_rects, r), text_rects))
    ex_rects = copy.deepcopy(exclude_rects)
    for r in ex_rects:
        r.x1 = r.x0+1
    rects.extend(ex_rects)
    rects.sort(key = lambda r: r.x0)

    # clusterize left sides of rects; height of a block is it's weight
    max_dist = min_col_width
    clusters = []
    cur_cluster = {}
    prev_left = -math.inf
    for r in rects:
        left = r.x0
        weight = r.y1 - r.y0
        if left - prev_left <= max_dist:
            cur_cluster['Weight'] += weight
        else:
            if cur_cluster != {}: clusters.append(cur_cluster)
            cur_cluster = {'Left' : left, 'Weight' : weight}
        prev_left = left
    clusters.append(cur_cluster)

    # penalize block crossings
    for r in rects:
        for c in clusters:
           if r.x0 < c['Left'] and r.x1 > c['Left']:
               c['Weight'] -= (r.y1 - r.y0)*block_crossing_penalty


    splits = [c['Left'] for c in clusters if c['Weight'] > 0]
    splits.append(clip.x1)
    prev_left = splits[0]
    columns = []
    for s in splits[1:]:
        columns.append((prev_left, s))
        prev_left = s
    
    return columns

def _get_font_size(span : dict) -> int:
    italic = 0 #bool(span['flags'] & 2)
    bold = bool(span['flags'] & 16)
    return round(100*span['size']) + 2*bold + 1*italic

def _detect_headers(
        doc          : pymupdf.Document,
        page_numbers : List[int],
        max_level    : Optional[int] = None
    ) -> Dict[int, int]: # font size -> level

    if max_level is None: max_level = 1000
    SPACES = set(string.whitespace)

    font_sizes = {}
    for page in [doc[pno] for pno in page_numbers]:
        text_page = page.get_textpage()
        blocks = text_page.extractDICT()['blocks']
        for b in blocks:
            for l in b['lines']:
                for span in l['spans']:
                    if not SPACES.issuperset(span['text']):
                        sz = _get_font_size(span)
                        if not sz in font_sizes:
                            font_sizes[sz] = 0
                        font_sizes[sz] += len(span['text'].strip())

    main_font_size = sorted(font_sizes.items(), key=lambda v: v[1], reverse=True)[0][0]

    headers = {}
    for level, sz in enumerate(sorted(font_sizes.keys(), reverse=True)):
        if sz <= main_font_size or level > max_level: break
        headers[sz] = level

    return headers

def pdf_to_tree(
        file_name        : str,
        page_numbers     : Optional[List[int]] = None,
        detect_columns   : Optional[bool] = False,
        max_header_level : Optional[int] = None
    ) -> Node:

    doc = pymupdf.open(file_name)
    if page_numbers is None : page_numbers = range(doc.page_count)

    headers = _detect_headers(doc, page_numbers=page_numbers, max_level=max_header_level)
    #print(headers)

    node_id = 0
    tree = Node(name=str(node_id), parent=None, level=-1, header=os.path.basename(file_name), text=doc.metadata['title'], page=page_numbers[0])
    prev_node = tree

    for page in [doc[pno] for pno in page_numbers]:
        text_page = page.get_textpage()
        blocks = text_page.extractDICT()['blocks']
        tables = page.find_tables()
        #table_rects = [ pymupdf.Rect(t.bbox) | pymupdf.Rect(t.header.bbox) for t in tables.tables ]
        table_rects = [ pymupdf.Rect(t.bbox) for t in tables.tables ]

        if detect_columns:
            text_rects  = [ pymupdf.Rect(b['bbox']) for b in blocks ]
            columns = _detect_columns(clip=page.rect, text_rects=text_rects, exclude_rects=table_rects)
        else:
            columns = [(page.rect.x0, page.rect.x1)]

        blocks = list(filter(lambda b: not _contain(table_rects, pymupdf.Rect(b['bbox'])), blocks))

        for t in tables.tables:
            blocks.append({'type': 3, 'bbox': t.bbox, 'table': t})

        for (left, right) in columns:
            other_cols_blocks, this_col_blocks = [], []
            for b in blocks:
                (other_cols_blocks, this_col_blocks)[b['bbox'][0] >= left and b['bbox'][0] < right].append(b)
            blocks = other_cols_blocks
            this_col_blocks.sort(key = lambda b: [b['bbox'][1], b['bbox'][0]])
            prev_y = 0
            for block in this_col_blocks:
                if block['type'] == 0: #text
                    for line in block['lines']:
                        y = (line['bbox'][1] + line['bbox'][3]) / 2
                        if prev_y != 0:
                            n = min(3, round(abs(y - prev_y) / line['spans'][0]['size']))
                            if n > 0:
                                tree.text += "\n" * n
                        prev_y = y

                        last_span_sz = _get_font_size(line['spans'][-1])
                        for i, span in enumerate(line['spans']):
                            text = _sanitize_text(span['text'])
                            sz = _get_font_size(span)
                            if i == 0 and sz in headers and sz == last_span_sz:
                                text = text.rstrip()
                                node_id += 1
                                level = headers[sz]
                                prev_y = 0

                                if level < tree.level:
                                    while not tree.is_root and tree.level > level:
                                        tree = tree.parent

                                if level == tree.level:
                                    if prev_node == tree and tree.text == "":
                                        tree.header += " " + text
                                    else:
                                        tree = Node(name=str(node_id), parent=tree.parent, level=level, header=text, text="", page=page.number)
                                else:
                                    tree = Node(name=str(node_id), parent=tree, level=level, header=text, text="", page=page.number)
                            else:
#                                text = resolve_links
                                tree.text += text
                            prev_node = tree
                elif block['type'] == 3: # table
                    table = block['table']
                    text = _sanitize_text(table.to_markdown(clean=False))
                    tree.text += f"\n\n{text}"

    root = tree.root
    for n in PreOrderIter(root):
        header = n.header.strip()
        text = n.text.strip()
        if n.is_leaf and (text == "" or text.isspace()) and util.rightsibling(n) is not None:
            util.rightsibling(n).header = header + " " + util.rightsibling(n).header
            n.parent = None
            continue
        n.header = header
        n.text = text

    return root
