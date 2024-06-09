import os
import asyncio
from aiopath import AsyncPath
import aiofiles
from typing import Optional, List, Tuple, Dict
from box import Box
from anytree import Node
from anytree.importer import JsonImporter

def get_document_id(file_name : str, spec : Box) -> str:
    assert file_name in spec
    return spec[file_name].id

def get_document_title(file_name : str, spec : Box) -> str:
    assert file_name in spec
    return spec[file_name].title

def get_documents(config : Box, spec : Box) -> List[Dict[str, str]]:
    docs = []
    for file_name in spec.keys():
        docs.append({'id': get_document_id(file_name, spec), 'file_name': file_name, 'title': get_document_title(file_name, spec)})
    return docs

async def get_trees(config : Box, spec : Box) -> Dict[str, Node]:
    tree_importer = JsonImporter()
    trees = {}
    async def read_tree(id: str):
        pdf_name  = config.path.pdfs  + id
        tree_name = config.path.trees + id + ".json"
        if os.path.isfile(tree_name):
            fh = await aiofiles.open(tree_name, "r")
            trees[id] = tree_importer.import_(await fh.read())
            await fh.close()
    ids = [doc['id'] for doc in get_documents(config, spec)]
    for read in asyncio.as_completed([read_tree(id) for id in ids]):
        await read
    return trees