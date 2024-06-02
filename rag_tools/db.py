import os
import glob
from typing import Optional, List, Tuple, Dict
from box import Box
from anytree import Node
from anytree.importer import JsonImporter

def get_documents(config : Box) -> List[Dict[str, str]]:
    docs = []
    for f in glob.glob(config.path.pdfs + "*.pdf"):
        file_name = os.path.basename(f) 
        docs.append({'id': file_name, 'file_name': file_name, 'title': file_name})
    return docs

def get_trees(config : Box) -> Dict[str, Node]:
    tree_importer = JsonImporter()
    trees = {}
    pdfs_fnames = [doc['file_name'] for doc in get_documents(config)]
    for pdf_fname in pdfs_fnames:
        pdf_name  = config.path.pdfs  + pdf_fname
        tree_name = config.path.trees + pdf_fname + ".json"
        if os.path.isfile(tree_name):
            fh = open(tree_name, "r")
            trees[pdf_fname] = tree_importer.read(fh)
            fh.close()
    return trees