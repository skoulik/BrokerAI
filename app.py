from flask import Flask, request, redirect, jsonify

app = Flask(__name__, static_url_path="", static_folder="frontend")

@app.route("/")
def index():
    return redirect("/index.html")

@app.route("/search", methods=["POST"])
def search():
    return {'result': "Response for: " + request.json['query']}