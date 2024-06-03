import rag_tools
from box import Box
import yaml
from quart import Quart, request, redirect, send_from_directory, jsonify
import atexit

config = Box.from_yaml(
    filename = "config.yaml",
    Loader   = yaml.FullLoader
)

strings_embedder = rag_tools.Embedder(config)

app = Quart(__name__, static_url_path="", static_folder="frontend", template_folder="frontend")

@app.route("/")
async def index():
    return redirect("/index.html")

@app.route('/pdfs/<path:path>')
async def static_pdf(path):
    return await send_from_directory(config.path.pdfs, path, mimetype="application/pdf")

@app.route("/documents", methods=["POST"])
async def list_docs():
    return {'documents': await rag_tools.get_documents(config)}

@app.route("/search", methods=["POST"])
async def search():
    return {'result': await strings_embedder.embed_strings([(await request.json)['query']])}


@atexit.register
def shutdown():
    return 0 #TODO


app.run()
