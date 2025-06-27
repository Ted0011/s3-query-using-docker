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
        "origins": ["http://localhost:8080", "http://frontend", "http://192.168.88.24:8080"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type"],
        "supports_credentials": True,
        "max_age": 600
    }
})

# Configure S3 client
s3 = boto3.client('s3', config=boto3.session.Config(
    connect_timeout=30,
    read_timeout=300,
    retries={'max_attempts': 3}
))
bucket_name = 'fintech-logs-prod'

class LargeScaleS3Searcher:
    def __init__(self, pin, date, selected_prefix):
        self.pin = pin
        self.date = date
        self.base_prefix = f"iPay/PayoutMiddleware/{selected_prefix.strip('/')}"
        self.prefix = f"{self.base_prefix}/{date}/" if date else f"{self.base_prefix}/"
        print(f"DEBUG: Searching with prefix: {self.prefix}", file=sys.stderr)  # Debug log
        self.matches = []
        self._closed = False
        self.last_keepalive = time.time()
        self.files_processed = 0
        self.last_progress_update = 0

    def __iter__(self):
        try:
            yield ": keepalive\n\n"
            yield "retry: 30000\n\n"
            
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
                    
                    if time.time() - self.last_keepalive > 15:
                        yield ": keepalive\n\n"
                        self.last_keepalive = time.time()
                    
                    key = obj['Key']
                    if not key.endswith('.txt'):
                        continue
                        
                    print(f"DEBUG: Processing file: {key}", file=sys.stderr)  # Debug log
                    
                    try:
                        response = s3.get_object(Bucket=bucket_name, Key=key)
                        body = response['Body']
                        
                        for line in body.iter_lines():
                            if self._closed:
                                return
                            
                            try:
                                decoded = line.decode('utf-8')
                                if (self.pin.lower() in decoded.lower() or 
                                    f'"{self.pin}"' in decoded or 
                                    f' {self.pin} ' in decoded):
                                    match = f"[{key}] {decoded}"
                                    self.matches.append(match)
                                    print(f"DEBUG: Found match in {key}", file=sys.stderr)  # Debug log
                                    yield f"data: {json.dumps({'result': match})}\n\n"
                            except UnicodeDecodeError:
                                try:
                                    decoded = line.decode('latin-1')
                                    if (self.pin.lower() in decoded.lower() or 
                                        f'"{self.pin}"' in decoded or 
                                        f' {self.pin} ' in decoded):
                                        match = f"[{key}] {decoded}"
                                        self.matches.append(match)
                                        yield f"data: {json.dumps({'result': match})}\n\n"
                                except Exception:
                                    continue
                        
                        self.files_processed += 1
                        
                        if self.files_processed % 10 == 0 or time.time() - self.last_progress_update > 10:
                            print(f"DEBUG: Progress - {self.files_processed} files, {len(self.matches)} matches", file=sys.stderr)
                            self.last_progress_update = time.time()
                            yield f"data: {json.dumps({'status': 'progress', 'files_processed': self.files_processed, 'matches_found': len(self.matches)})}\n\n"
                            
                    except Exception as e:
                        print(f"ERROR processing {key}: {str(e)}", file=sys.stderr)
                        continue
            
            if not self._closed:
                print(f"DEBUG: Search complete. Total: {self.files_processed} files, {len(self.matches)} matches", file=sys.stderr)
                yield f"data: {json.dumps({'status': 'complete', 'count': len(self.matches), 'files_processed': self.files_processed})}\n\n"
                
        except Exception as e:
            if not self._closed:
                print(f"ERROR in search: {str(e)}", file=sys.stderr)
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
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
        selected_prefix = data.get('prefix', 'SmartSwift')
    else:  # GET
        pin = request.args.get('pin')
        date = request.args.get('date')
        selected_prefix = request.args.get('prefix', 'SmartSwift')

    print(f"DEBUG: Received search request - Prefix: {selected_prefix}, PIN: {pin}, Date: {date}", file=sys.stderr)  # Debug log

    if not pin:
        return jsonify({'error': 'PIN parameter is required'}), 400

    generator = LargeScaleS3Searcher(pin, date, selected_prefix)
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
