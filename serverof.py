from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from hypercorn.config import Config
from hypercorn.asyncio import serve
from pydantic import BaseModel
import asyncio
import logging
from typing import List, Optional
import time
import os
from dotenv import load_dotenv
import json
import random
import string

# Load environment variables
load_dotenv()

try:
    import poe
except ImportError:
    import subprocess
    import sys

    subprocess.check_call([sys.executable, "-m", "pip", "install", "poe-api"])
    import poe

# Set up logging
logging.basicConfig(filename="app.log", level=logging.INFO)

app = FastAPI()

origins = [
    "https://localhost",  
    "https://bettergpt.chat",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True, 
    allow_methods=["*"], 
    allow_headers=["*"],
)

MODEL_MAPPING = {
    "sage": "capybara",
    "claude-instant": "a2",
    "claude-2-100k": "a2_2",
    "claude-instant-100k": "a2_100k",
    "gpt-3.5-turbo-0613": "chinchilla",
    "gpt-3.5-turbo": "chinchilla",
    "gpt-3.5-turbo-16k-0613": "agouti",
    "gpt-3.5-turbo-16k": "agouti",
    "gpt-4": "beaver",
    "gpt-4-0613": "beaver",
    "gpt-4-32k": "vizcacha",
    "chat-bison-001": "acouchy",
}

class Message(BaseModel):
    role: str
    content: str

# Add stream parameter to your Messages model
class Messages(BaseModel):
    model: str
    messages: List[Message]
    stream: Optional[bool] = False  # By default, it's set to False

class CompletionPayload(BaseModel):
    prompt: str
    max_tokens: int
    temperature: float
    presence_penalty: int
    top_p: int

class PoeResponse(BaseModel):
    choices: List[Message]

class PoeProvider:
    def __init__(
        self,
        POE_TOKENS: list = None,
        PROXIES: list = None,
        AI_MODEL: str = "chinchilla",
        MAX_CONCURRENT_REQUESTS: int = 100,  # Set your limit here
        **kwargs,
    ):
        self.POE_TOKENS = POE_TOKENS or []
        self.PROXIES = PROXIES or []
        self.bad_tokens = []
        self.AI_MODEL = AI_MODEL.lower()
        self.current_token_index = 0
        self.current_proxy_index = 0
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)  # Create the Semaphore

        # Create a single poe.Client instance
        self.client = poe.Client(token=self._get_current_token(), proxy=self._get_current_proxy())

    def set_model(self, model: str):
        if model in MODEL_MAPPING:
            self.AI_MODEL = MODEL_MAPPING[model]
        else:
            self.AI_MODEL = model

    def _get_current_token(self):
        return self.POE_TOKENS[self.current_token_index]

    def _get_current_proxy(self):
        return self.PROXIES[self.current_proxy_index]
    
    def get_current_proxy(self):
        return self.PROXIES[self.current_proxy_index]

    def _rotate_token(self):
        if len(self.bad_tokens) == len(self.POE_TOKENS):
            self.bad_tokens = []  # Reset the bad tokens list if all tokens have been marked as bad

    def rotate_token(self):
        if len(self.bad_tokens) == len(self.POE_TOKENS):
            self.bad_tokens = []  # Reset the bad tokens list if all tokens have been marked as bad

        self.current_token_index = (self.current_token_index + 1) % len(self.POE_TOKENS)
        while self._get_current_token() in self.bad_tokens:  # Skip over bad tokens
            self.current_token_index = (self.current_token_index + 1) % len(self.POE_TOKENS)

        self.client.token = self._get_current_token()  # Update the client's token


    def _rotate_proxy(self):
        self.current_proxy_index = (self.current_proxy_index + 1) % len(self.PROXIES)
        self.client.proxy = self._get_current_proxy()  # Update the client's proxy

    
    def rotate_proxy(self):
        self.current_proxy_index = (self.current_proxy_index + 1) % len(self.PROXIES)
        self.client.proxy = self._get_current_proxy()  # Update the client's proxy

    
    async def instruct(self, messages: List[Message], tokens: int = 0, max_retries=3):
        async with self.semaphore:  # Use the Semaphore here
            for i in range(max_retries):
                try:
                    self._rotate_proxy()  # Rotate the proxy for every request

                    if self.AI_MODEL not in self.client.bot_names:
                        self.AI_MODEL = self.client.get_bot_by_codename(self.AI_MODEL)
                    
                    last_user_message = [msg for msg in messages if msg.role == "user"][-1].content

                    if last_user_message.strip():  # Check if the message is not empty
                        for chunk in self.client.send_message(
                            chatbot=self.AI_MODEL, message=last_user_message
                        ):
                            pass
                        return {"role": "assistant", "content": chunk["text"]}
                    else:
                        logging.warning("Attempted to send an empty message, skipping.")
                        return {"role": "assistant", "content": ""}
                    
                except Exception as e:  # Catch all other exceptions
                    logging.error(f"Unexpected error during instruction: {str(e)}")
                    self._rotate_token()
                    await asyncio.sleep(2**i)  # Exponential backoff

            raise HTTPException(
                status_code=429, detail="Rate limit exceeded despite retries"
            )

    async def test_tokens(self):
        """Test all tokens by making a simple request."""
        for idx, token in enumerate(self.POE_TOKENS):
            self.current_token_index = idx
            self.client.token = self._get_current_token()
            try:
                # Make a simple request to test the token
                self.client.get_bot_by_codename(self.AI_MODEL)
            except Exception:
                # If an error occurs, mark the token as bad
                self.bad_tokens.append(token)


poe_provider = None


def generate_id():
    characters = string.ascii_letters + string.digits  # this includes both lower and uppercase letters and digits
    random_part = ''.join(random.choice(characters) for _ in range(29))  # generate a string of 29 random characters
    return f"chatcmpl-{random_part}"

@app.on_event("startup")
async def startup_event():
    global poe_provider
    poe_provider = PoeProvider(
        POE_TOKENS=os.getenv("POE_TOKENS").split(","),
        PROXIES=os.getenv("PROXIES").split(","),
        AI_MODEL="vizcacha",
        MAX_CONCURRENT_REQUESTS=50  # Set your limit here
    )
    # Rotate tokens and proxies at startup
    for _ in range(len(poe_provider.POE_TOKENS)):
        poe_provider.rotate_token()
    for _ in range(len(poe_provider.PROXIES)):
        poe_provider.rotate_proxy()

async def stream_response(data):
    if isinstance(data, dict):
        first_chunk = True
        words = data['choices'][0]['message']['content'].split()
        for i in range(len(words)):
            word = words[i] + ' ' if i != len(words) - 1 else words[i]
            chunk = {
                'id': data['id'],
                'object': data['object'],
                'created': data['created'],
                'model': data['model'],
                'choices': [
                    {
                        'index': data['choices'][0]['index'],
                        'delta': {'role': 'assistant', 'content': word} if first_chunk else {'content': word},
                        'finish_reason': None
                    }
                ]
            }
            yield f'data: {json.dumps(chunk)}\n\n'
            if first_chunk:
                first_chunk = False
        
        # Add a final chunk to signify completion
        done_chunk = {
            'id': data['id'],
            'object': data['object'],
            'created': data['created'],
            'model': data['model'],
            'choices': [
                {
                    'index': data['choices'][0]['index'],
                    'delta': {},
                    'finish_reason': 'stop'
                }
            ]
        }
        yield f'data: {json.dumps(done_chunk)}\n\n'
        yield 'data: [DONE]\n\n'
    else:
        for chunk in data:
            yield f'data: {json.dumps(chunk)}\n\n'
        yield 'data: [DONE]\n\n'


@app.post("/v1/chat/completions", status_code=status.HTTP_200_OK)
async def generate_chat_response(request: Request):
    try:
        # Parse the incoming stream as JSON
        messages = await request.json()

        # Validate the input data
        messages = Messages(**messages)

        # Set the model in the provider
        poe_provider.set_model(messages.model)

        # Generate the response
        response_message = await poe_provider.instruct(messages=messages.messages)

        response_data = {
            'id': generate_id(),
            'object': 'chat.completion',
            'created': int(time.time()),
            'model': messages.model,
            'choices': [
                {
                    'index': 0,
                    'message': {
                        'role': 'assistant',
                        'content': response_message['content']
                    },
                    'finish_reason': 'stop'
                }
            ]
        }

        # Use the stream_response function to send the data in chunks if streaming is enabled
        if messages.stream:
            response_data['object'] = 'chat.completion.chunk'
            return StreamingResponse(stream_response(response_data), media_type='text/event-stream')
        else:
            return response_data

    except HTTPException as e:
        logging.error(f"Error during response generation: {str(e)}")
        raise e
    except Exception as e:
        logging.error(f"Unhandled exception: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/engines/{model}/completions", status_code=status.HTTP_200_OK, response_class=StreamingResponse)
async def generate_completion(request: Request, model: str, payload: CompletionPayload):
    messages = [
        Message(role="user", content=payload.prompt)
    ]
    try:
        # Set the model in the provider
        poe_provider.set_model(model)

        response_message = await poe_provider.instruct(messages=messages)

        return {
            'id': generate_id(),
            'object': 'text.completion',
            'created': int(time.time()),
            'model': model,
            'choices': [{
                'text': response_message['content'],
                'index': 0
            }]
        }
    except HTTPException as e:
        logging.error(f"Error during response generation: {str(e)}")
        raise e
    except Exception as e:
        logging.error(f"Unhandled exception: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    config = Config()
    config.bind = ["0.0.0.0:8000"]  # The IP and port to bind to
    asyncio.run(serve(app, config))