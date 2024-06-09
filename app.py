import rag_tools
import asyncio
from box import Box
import yaml
from quart import Quart, request, redirect, send_from_directory, jsonify
import chromadb
import chromadb.config

app = Quart(__name__, static_url_path="", static_folder="frontend", template_folder="frontend")

config = Box.from_yaml(
    filename = "config.yaml",
    Loader   = yaml.FullLoader
)

spec = Box.from_yaml(
    filename = config.path.pdfs + "spec.yaml",
    Loader   = yaml.FullLoader
)

pdfs = rag_tools.get_documents(config, spec)
trees = asyncio.run(rag_tools.get_trees(config, spec))

strings_embedder = rag_tools.Embedder(config)
chromadb_client = chromadb.PersistentClient(
    path     = config.path.embeddings,
    settings = chromadb.config.Settings(anonymized_telemetry = False)
)

collections = {}
for doc in pdfs:
    docId = doc['id']
    collections[docId] = chromadb_client.get_collection(name=docId)


@app.route("/")
async def index():
    return redirect("/index.html")

@app.route('/pdfs/<path:path>')
async def static_pdf(path):
    return await send_from_directory(config.path.pdfs, path, mimetype="application/pdf")

@app.route("/documents", methods=["POST"])
async def list_docs():
    return {'documents': pdfs}

@app.route("/search", methods=["POST"])
async def search():
    data = await request.json;
    docId = data['docId']
    query = data['query']
    num_results = max(data['num_results'], 1)
    embedding = (await strings_embedder.embed_strings([query]))[0]
    #if docId not in collections:
        #TODO
    query_results = collections[docId].query(
        query_embeddings = [embedding],
        n_results = num_results
    )
    n_results = len(query_results['ids'][0])
    results = []
    for i in range(n_results):
        path = query_results['ids'][0][i]
        crumbs = []
        node = rag_tools.walk_tree(
            root = trees[docId],
            path = path.split('#')[0],
            func = lambda node: crumbs.append(node.header)
        )
        results.append({
            'crumbs'   : crumbs,
            'text'     : node.text,
            'page'     : node.page,
            'position' : node.position,
            'relevance': 1 - query_results['distances'][0][i]
        })
    return {
        'docId'  : docId,
        'query'  : query,
        'results': results,
    };


app.run(threaded=False)