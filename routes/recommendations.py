"""
More Like This API
Provides recommendations similar to a given movie/TV show
Uses genre, cast, director matching logic
"""
from flask import Blueprint, jsonify, request
from models import MediaItem
import os
import requests

recommendations_bp = Blueprint('recommendations', __name__)

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"


@recommendations_bp.route('/api/media/<int:media_id>/recommendations', methods=['GET'])
def get_recommendations(media_id):
    """
    Get similar movies/TV shows recommendations
    Uses TMDB's recommendation and similar endpoints
    """
    try:
        media_type = request.args.get('media_type', 'movie')
        limit = request.args.get('limit', 12, type=int)
        
        if media_type not in ['movie', 'tv']:
            return jsonify({'error': 'Invalid media_type. Must be movie or tv'}), 400
        
        # Try TMDB recommendations endpoint first
        rec_url = f"{TMDB_BASE_URL}/{media_type}/{media_id}/recommendations"
        rec_response = requests.get(rec_url, params={
            'api_key': TMDB_API_KEY,
            'language': 'en-US',
            'page': 1
        })
        
        recommendations = []
        if rec_response.status_code == 200:
            rec_data = rec_response.json()
            recommendations = rec_data.get('results', [])
        
        # If not enough recommendations, try similar endpoint
        if len(recommendations) < limit:
            similar_url = f"{TMDB_BASE_URL}/{media_type}/{media_id}/similar"
            similar_response = requests.get(similar_url, params={
                'api_key': TMDB_API_KEY,
                'language': 'en-US',
                'page': 1
            })
            
            if similar_response.status_code == 200:
                similar_data = similar_response.json()
                similar_items = similar_data.get('results', [])
                
                # Add similar items that aren't already in recommendations
                existing_ids = {item['id'] for item in recommendations}
                for item in similar_items:
                    if item['id'] not in existing_ids:
                        recommendations.append(item)
                        existing_ids.add(item['id'])
                    
                    if len(recommendations) >= limit:
                        break
        
        # Limit to requested number
        recommendations = recommendations[:limit]
        
        # Format response
        formatted_recommendations = []
        for item in recommendations:
            formatted_item = {
                'id': item['id'],
                'media_type': media_type,
                'title': item.get('title') or item.get('name'),
                'overview': item.get('overview', ''),
                'poster_path': f"https://image.tmdb.org/t/p/w500{item['poster_path']}" if item.get('poster_path') else None,
                'backdrop_path': f"https://image.tmdb.org/t/p/w1280{item['backdrop_path']}" if item.get('backdrop_path') else None,
                'release_date': item.get('release_date') or item.get('first_air_date'),
                'vote_average': item.get('vote_average', 0),
                'vote_count': item.get('vote_count', 0),
                'genre_ids': item.get('genre_ids', [])
            }
            formatted_recommendations.append(formatted_item)
        
        return jsonify({
            'success': True,
            'recommendations': formatted_recommendations,
            'total': len(formatted_recommendations)
        })
        
    except requests.RequestException as e:
        return jsonify({'success': False, 'error': f'TMDB API error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
