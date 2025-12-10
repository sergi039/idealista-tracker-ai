#!/usr/bin/env python3
"""
Debug Claude AI response parsing issue
"""

import os
import sys
sys.path.insert(0, '.')

from services.anthropic_service import get_anthropic_service
import json

def test_claude_response():
    """Test Claude response to see what format it's returning"""
    
    try:
        anthropic_service = get_anthropic_service()
        
        # Simple test prompt
        prompt = """Create a JSON response with the following format:
{
    "enhanced_description": "Test description",
    "key_highlights": ["highlight 1", "highlight 2"],
    "confidence_score": 0.8
}

Please return ONLY the JSON, no extra text or markdown."""
        
        print("=== TESTING CLAUDE RESPONSE FORMAT ===")
        print("Sending prompt...")
        
        message = anthropic_service.client.messages.create(
            model="claude-3-5-sonnet-20241022",  # Use the latest model
            max_tokens=400,
            temperature=0.1,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )
        
        # Extract response
        response_text = ""
        if message.content and len(message.content) > 0:
            content_block = message.content[0]
            if hasattr(content_block, 'text') and content_block.text:
                response_text = content_block.text
        
        print(f"Raw response: '{response_text}'")
        print(f"Response type: {type(response_text)}")
        print(f"Response length: {len(response_text)}")
        
        # Try to parse as JSON
        try:
            parsed = json.loads(response_text)
            print("JSON parsing: SUCCESS")
            print(f"Parsed data: {parsed}")
        except json.JSONDecodeError as e:
            print(f"JSON parsing: FAILED - {e}")
            
            # Try to extract JSON from markdown code blocks
            import re
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if json_match:
                json_text = json_match.group(1)
                print(f"Found JSON in code block: '{json_text}'")
                try:
                    parsed = json.loads(json_text)
                    print("Extracted JSON parsing: SUCCESS")
                    print(f"Parsed data: {parsed}")
                except json.JSONDecodeError as e2:
                    print(f"Extracted JSON parsing: FAILED - {e2}")
            else:
                print("No JSON code block found")
        
    except Exception as e:
        print(f"ERROR: {str(e)}")

if __name__ == "__main__":
    test_claude_response()