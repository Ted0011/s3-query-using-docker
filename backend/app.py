from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import boto3
import json
import time
import sys
from contextlib import closing

app = Flask(__name__)

# Enhanced CORS configuration
CORS(app, resources={
    r"/*": {
        "origins": ["http://localhost:8080", "http://frontend"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"],
        "supports_credentials": True,
        "max_age": 600
    }
})

# Configure S3 client with longer timeouts
s3 = boto3.client('s3', config=boto3.session.Config(
    connect_timeout=30,  # 30 seconds connection timeout
    read_timeout=300,    # 5 minutes read timeout
    retries={'max_attempts': 3}
))
bucket_name = 'fintech-logs-prod'
base_prefix = 'iPay/PayoutMiddleware/SmartSwift'

class LargeScaleS3Searcher:
    def __init__(self, pin, date):
        self.pin = pin
        self.date = date
        self.prefix = f"{base_prefix}/{date}/" if date else f"{base_prefix}/"
        self.matches = []
        self._closed = False
        self.last_keepalive = time.time()
        self.files_processed = 0
        self.last_progress_update = 0

    def __iter__(self):
        try:
            # Initial headers
            yield ": keepalive\n\n"
            yield "retry: 30000\n\n"  # 30 second retry
            
            paginator = s3.get_paginator('list_objects_v2')
            page_iterator = paginator.paginate(
                Bucket=bucket_name,
                Prefix=self.prefix,
                PaginationConfig={'PageSize': 1000}
            )
            
            for page in page_iterator:
                if self._closed:
                    return
                
                for obj in page.get('Contents', []):
                    if self._closed:
                        return
                    
                    # Send keepalive every 15 seconds
                    if time.time() - self.last_keepalive > 15:
                        yield ": keepalive\n\n"
                        self.last_keepalive = time.time()
                    
                    key = obj['Key']
                    if not key.endswith('.txt'):
                        continue
                        
                    try:
                        # Process file without closing context since S3 response is a dict
                        response = s3.get_object(Bucket=bucket_name, Key=key)
                        body = response['Body']
                        
                        for line in body.iter_lines():
                            if self._closed:
                                return
                            
                            decoded = line.decode('utf-8')
                            if self.pin in decoded:
                                match = f"[{key}] {decoded}"
                                self.matches.append(match)
                                try:
                                    yield f"data: {json.dumps({'result': match})}\n\n"
                                except (BrokenPipeError, GeneratorExit):
                                    self._closed = True
                                    return
                        
                        self.files_processed += 1
                        
                        # Log progress every 100 files (reduced from 1000 for more frequent updates)
                        if self.files_processed % 100 == 0 or time.time() - self.last_progress_update > 30:
                            print(f"Processed {self.files_processed} files, found {len(self.matches)} matches", file=sys.stderr)
                            self.last_progress_update = time.time()
                            yield f"data: {json.dumps({'status': 'progress', 'files_processed': self.files_processed, 'matches_found': len(self.matches)})}\n\n"
                            
                    except Exception as e:
                        print(f"Error processing {key}: {str(e)}", file=sys.stderr)
                        continue
            
            # Final status
            if not self._closed:
                yield f"data: {json.dumps({'status': 'complete', 'count': len(self.matches), 'files_processed': self.files_processed})}\n\n"
                
        except Exception as e:
            if not self._closed:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                print(f"Search error: {str(e)}", file=sys.stderr)
        finally:
            if not self._closed:
                yield "event: close\ndata: {}\n\n"
            self._closed = True

    def close(self):
        self._closed = True

@app.route('/search', methods=['GET', 'POST', 'OPTIONS'])
def search():
    if request.method == 'OPTIONS':
        return jsonify({'status': 'preflight'}), 200
    
    # Get parameters
    if request.method == 'POST':
        data = request.get_json()
        pin = data.get('pin')
        date = data.get('date')
    else:  # GET
        pin = request.args.get('pin')
        date = request.args.get('date')

    if not pin:
        return jsonify({'error': 'PIN parameter is required'}), 400

    generator = LargeScaleS3Searcher(pin, date)
    response = Response(
        generator,
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )
    response.call_on_close(generator.close)
    return response

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, threaded=True)
