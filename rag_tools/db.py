import os
import asyncio
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
    async def read_tree(id: str):
        pdf_name  = config.path.pdfs  + id
        tree_name = config.path.trees + id + ".json"
        if os.path.isfile(tree_name):
            fh = await aiofiles.open(tree_name, "r")
            trees[id] = tree_importer.import_(await fh.read())
            await fh.close()
    ids = [doc['id'] for doc in await get_documents(config)]
    for read in asyncio.as_completed([read_tree(id) for id in ids]):
        await read
    return trees