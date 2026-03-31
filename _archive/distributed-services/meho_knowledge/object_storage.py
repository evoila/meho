"""
S3-compatible object storage for documents.

Uses boto3 to interact with MinIO or AWS S3.
"""
# mypy: disable-error-code="no-untyped-def,no-any-return"
import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError
from meho_core.config import get_config
from meho_core.errors import MehoError
from typing import Optional


class ObjectStorage:
    """S3-compatible object storage for documents"""
    
    def __init__(self):
        """Initialize object storage client"""
        config = get_config()
        
        # Create S3 client (works with both MinIO and AWS S3)
        # Add http:// or https:// prefix based on use_ssl flag if not already present
        endpoint = config.object_storage_endpoint
        if not endpoint.startswith(('http://', 'https://')):
            endpoint = f"https://{endpoint}" if config.object_storage_use_ssl else f"http://{endpoint}"
        
        self.client = boto3.client(
            's3',
            endpoint_url=endpoint,
            aws_access_key_id=config.object_storage_access_key,
            aws_secret_access_key=config.object_storage_secret_key,
            config=BotoConfig(signature_version='s3v4'),
            region_name='us-east-1',  # Required for MinIO
        )
        
        self.bucket = config.object_storage_bucket
        self._ensure_bucket()
    
    def _ensure_bucket(self) -> None:
        """Create bucket if it doesn't exist"""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code')
            if error_code == '404':
                # Bucket doesn't exist, create it
                try:
                    self.client.create_bucket(Bucket=self.bucket)
                except ClientError as create_error:
                    # Ignore error if bucket was created by another process
                    if create_error.response.get('Error', {}).get('Code') != 'BucketAlreadyOwnedByYou':
                        raise MehoError(f"Failed to create bucket: {create_error}")
            else:
                raise MehoError(f"Failed to check bucket: {e}")
    
    def upload_document(
        self,
        file_bytes: bytes,
        key: str,
        content_type: Optional[str] = None
    ) -> str:
        """
        Upload document to storage.
        
        Args:
            file_bytes: Document content
            key: Storage key (path)
            content_type: MIME type (optional)
        
        Returns:
            Storage URI (s3://bucket/key format)
        """
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=file_bytes,
                ContentType=content_type or 'application/octet-stream'
            )
            return f"s3://{self.bucket}/{key}"
        except ClientError as e:
            raise MehoError(f"Failed to upload document: {e}")
    
    def download_document(self, key: str) -> bytes:
        """
        Download document from storage.
        
        Args:
            key: Storage key (path)
        
        Returns:
            Document content as bytes
        """
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response['Body'].read()
        except ClientError as e:
            raise MehoError(f"Failed to download document: {e}")
    
    def delete_document(self, key: str) -> None:
        """
        Delete document from storage.
        
        Args:
            key: Storage key (path)
        """
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            raise MehoError(f"Failed to delete document: {e}")
    
    def document_exists(self, key: str) -> bool:
        """
        Check if document exists.
        
        Args:
            key: Storage key (path)
        
        Returns:
            True if document exists, False otherwise
        """
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False
