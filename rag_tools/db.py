import os
from aiopath import AsyncPath
import aiofiles
from typing import Optional, List, Tuple, Dict
from box import Box
from anytree import Node
from anytree.importer import JsonImporter

async def get_documents(config : Box) -> List[Dict[str, str]]:
    docs = []
    pdfs_path = AsyncPath(config.path.pdfs)
    async for f in pdfs_path.glob("*.pdf"):
        file_name = os.path.basename(f) 
        docs.append({'id': file_name, 'file_name': file_name, 'title': file_name})
    return docs

async def get_trees(config : Box) -> Dict[str, Node]:
    tree_importer = JsonImporter()
    trees = {}
    pdfs_fnames = [doc['file_name'] for doc in await get_documents(config)]
    for pdf_fname in pdfs_fnames: #TODO make async
        pdf_name  = config.path.pdfs  + pdf_fname
        tree_name = config.path.trees + pdf_fname + ".json"
        if os.path.isfile(tree_name):
            fh = await aiofiles.open(tree_name, "r")
            trees[pdf_fname] = tree_importer.import_(await fh.read())
            await fh.close()
    return trees