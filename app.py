from flask import Flask, request, jsonify
import os
import win32com.client
import pythoncom
import logging

app = Flask(__name__)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.route('/')
def home():
    return "Welcome to the Visio to SVG Converter API. Use the /convert endpoint to convert files."

@app.route('/convert', methods=['POST'])
def convert_visio_to_svg():
    # Parse the JSON payload from the request
    data = request.json
    input_files = data.get('input_files')
    output_dir = data.get('output_dir')

    if not input_files or not output_dir:
        logger.error("Input files or output directory not provided.")
        return jsonify({'error': 'Please provide both input files and output directory'}), 400

    try:
        # Initialize COM library
        pythoncom.CoInitialize()

        visio = win32com.client.Dispatch("Visio.Application")
        visio.Visible = False

        result = []

        for index, input_file in enumerate(input_files):
            input_file = input_file.strip()
            if os.path.exists(input_file):
                filename = os.path.basename(input_file)
                base_name = os.path.splitext(filename)[0]

                try:
                    input_file = os.path.abspath(input_file)
                    output_file = os.path.abspath(output_dir)
                    logger.info(f"Processing file: {input_file}")

                    doc = visio.Documents.Open(input_file)

                    for i, page in enumerate(doc.Pages):
                        svg_path = os.path.join(output_file, f"{base_name}_Page_{i+1}.svg")
                        page.Export(svg_path)
                        result.append({'input_file': input_file, 'output_file': svg_path})
                        logger.info(f"Exported {svg_path}")

                    doc.Close()
                except Exception as e:
                    logger.error(f"Error processing {filename}: {e}")
                    return jsonify({'error': f"Error processing {filename}: {e}"}), 500
            else:
                logger.error(f"File not found: {input_file}")
                return jsonify({'error': f"File not found: {input_file}"}), 404

        visio.Quit()
        logger.info("Conversion completed successfully.")
        return jsonify({'result': result}), 200
    except Exception as e:
        logger.error(f"An error occurred with Visio application: {e}")
        return jsonify({'error': f"An error occurred with Visio application: {e}"}), 500
    finally:
        # Uninitialize COM library
        pythoncom.CoUninitialize()

if __name__ == '__main__':
    app.run(debug=True, port=5000)
