"""
Base utilities for mock systems.

Provides common in-memory storage and helpers.
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, UTC
import uuid
from threading import Lock


class InMemoryStore:
    """Thread-safe in-memory storage for mock systems"""
    
    def __init__(self):
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
    
    def create(self, collection: str, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create an item in a collection.
        
        Args:
            collection: Collection name
            item: Item data (will be assigned an ID if not present)
        
        Returns:
            Created item with ID
        """
        with self._lock:
            if collection not in self._data:
                self._data[collection] = {}
            
            # Assign ID if not present
            if "id" not in item:
                item["id"] = str(uuid.uuid4())
            
            # Add timestamps if not present
            if "created_at" not in item:
                item["created_at"] = datetime.now(UTC).isoformat()
            if "updated_at" not in item:
                item["updated_at"] = datetime.now(UTC).isoformat()
            
            self._data[collection][item["id"]] = item.copy()
            return item
    
    def get(self, collection: str, item_id: str) -> Optional[Dict[str, Any]]:
        """Get an item by ID"""
        with self._lock:
            if collection not in self._data:
                return None
            return self._data[collection].get(item_id)
    
    def list(
        self,
        collection: str,
        filter_fn: Optional[callable] = None
    ) -> List[Dict[str, Any]]:
        """
        List items in a collection with optional filtering.
        
        Args:
            collection: Collection name
            filter_fn: Optional filter function (item -> bool)
        
        Returns:
            List of items
        """
        with self._lock:
            if collection not in self._data:
                return []
            
            items = list(self._data[collection].values())
            
            if filter_fn:
                items = [item for item in items if filter_fn(item)]
            
            return items
    
    def update(self, collection: str, item_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update an item"""
        with self._lock:
            if collection not in self._data or item_id not in self._data[collection]:
                return None
            
            item = self._data[collection][item_id]
            item.update(updates)
            item["updated_at"] = datetime.now(UTC).isoformat()
            return item
    
    def delete(self, collection: str, item_id: str) -> bool:
        """Delete an item"""
        with self._lock:
            if collection not in self._data or item_id not in self._data[collection]:
                return False
            
            del self._data[collection][item_id]
            return True
    
    def clear(self, collection: Optional[str] = None):
        """Clear all data or specific collection"""
        with self._lock:
            if collection:
                self._data[collection] = {}
            else:
                self._data = {}


def generate_id(prefix: str = "") -> str:
    """Generate a unique ID with optional prefix"""
    return f"{prefix}{uuid.uuid4()}"


def check_api_key(api_key: Optional[str]) -> bool:
    """Simple API key validation for mocks"""
    # Accept any non-empty key or no key (for testing)
    return True  # Mocks are permissive for testing

