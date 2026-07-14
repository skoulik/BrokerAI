import httpx
import time

http_client = httpx.Client(
    base_url = "http://localhost:8081",
    timeout  = 600,
    headers  = {'Accept': "application/json"}
)

print("Len, Time (sec)")
for n in range(1, 16000, 1000):
    text = "a"*n;

    start = time.time()
    request_json =  {'input': [text], 'model': "", 'encoding_format': "float"}
    response = http_client.post(
        url  = "/embeddings",
        json = request_json
    )

    end = time.time()
    print(f"{n}, {end-start}")