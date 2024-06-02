import rag_tools
from box import Box
import yaml
from flask import Flask, request, redirect, send_from_directory, jsonify

config = Box.from_yaml(
    filename = "config.yaml",
    Loader   = yaml.FullLoader
)

app = Flask(__name__, static_url_path="", static_folder="frontend")

@app.route("/")
def index():
    return redirect("/index.html")

@app.route('/pdfs/<path:path>')
def static_pdf(path):
    return send_from_directory(config.path.pdfs, path, mimetype="application/pdf")

@app.route("/documents", methods=["POST"])
def list_docs():
    return {'documents': rag_tools.get_documents(config)}


strings_embedder = rag_tools.Embedder(config)

@app.route("/search", methods=["POST"])
def search():
    return {'result': strings_embedder.embed_strings([request.json['query']])}