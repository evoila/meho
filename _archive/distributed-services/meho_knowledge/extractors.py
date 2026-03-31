"""
Text extraction from various document formats.

Supports PDF, DOCX, HTML, and plain text.
"""
# mypy: disable-error-code="return-value"
from typing import List, Protocol
from abc import abstractmethod
import io


class TextExtractor(Protocol):
    """Protocol for text extractors"""
    
    @abstractmethod
    def extract(self, file_bytes: bytes) -> List[str]:
        """
        Extract text from file.
        
        Args:
            file_bytes: File content as bytes
        
        Returns:
            List of text strings (e.g., pages or sections)
        """
        ...


class PDFExtractor:
    """Extract text from PDF files"""
    
    def extract(self, file_bytes: bytes) -> List[str]:
        """
        Extract text page by page from PDF.
        
        Args:
            file_bytes: PDF file content
        
        Returns:
            List of page texts
        
        Raises:
            ValueError: If PDF is corrupted, password-protected, or cannot be read
        """
        import pypdf
        
        try:
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            
            # Check if PDF is encrypted/password-protected
            if reader.is_encrypted:
                raise ValueError("PDF is password-protected. Please provide an unencrypted version.")
            
            pages = []
            
            # IMPORTANT: Always append to preserve page numbering
            # Use empty string for failed pages to maintain alignment
            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text()
                    if text and text.strip():
                        pages.append(text)
                    else:
                        # Empty page - keep it to preserve page numbers
                        pages.append("")
                except Exception as e:
                    # Failed extraction - use empty string to preserve page numbering
                    # This ensures page N in source_uri matches actual PDF page N
                    # Could log warning: f"Failed to extract page {page_num}: {e}"
                    pages.append("")
            
            return pages if pages else [""]  # Return at least one empty string
        
        except pypdf.errors.PdfReadError as e:
            raise ValueError(f"Corrupted or invalid PDF file: {e}") from e
        except Exception as e:
            # Catch any other PDF-related errors
            if "password" in str(e).lower() or "encrypted" in str(e).lower():
                raise ValueError("PDF is password-protected or encrypted") from e
            raise ValueError(f"Failed to read PDF: {e}") from e


class TextFileExtractor:
    """Extract text from plain text files"""
    
    def extract(self, file_bytes: bytes) -> List[str]:
        """
        Decode text file.
        
        Args:
            file_bytes: Text file content
        
        Returns:
            List with single text string
        """
        try:
            text = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            # Try other encodings
            try:
                text = file_bytes.decode('latin-1')
            except UnicodeDecodeError:
                text = file_bytes.decode('utf-8', errors='ignore')
        
        return [text]


class DocxExtractor:
    """Extract text from Word documents"""
    
    def extract(self, file_bytes: bytes) -> List[str]:
        """
        Extract text paragraph by paragraph from DOCX.
        
        Args:
            file_bytes: DOCX file content
        
        Returns:
            List of paragraph texts
        
        Raises:
            ValueError: If DOCX is corrupted or cannot be read
        """
        from docx import Document
        from docx.opc.exceptions import PackageNotFoundError
        
        try:
            doc = Document(io.BytesIO(file_bytes))
            paragraphs = []
            
            for para in doc.paragraphs:
                if para.text and para.text.strip():
                    paragraphs.append(para.text)
            
            return paragraphs if paragraphs else [""]
        
        except PackageNotFoundError as e:
            raise ValueError("Invalid or corrupted DOCX file") from e
        except Exception as e:
            # Catch any other document parsing errors
            error_msg = str(e).lower()
            if "corrupt" in error_msg or "invalid" in error_msg:
                raise ValueError(f"Corrupted DOCX file: {e}") from e
            raise ValueError(f"Failed to read DOCX: {e}") from e


class HTMLExtractor:
    """Extract text from HTML, preserving structure via markdown conversion"""
    
    def extract(self, file_bytes: bytes) -> List[str]:
        """
        Extract text from HTML, converting headings to markdown format.
        
        This preserves document structure so heading hierarchy can be tracked
        for metadata extraction.
        
        Args:
            file_bytes: HTML file content
        
        Returns:
            List with single text string (headings converted to markdown)
        """
        from bs4 import BeautifulSoup, NavigableString
        
        try:
            html = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            html = file_bytes.decode('utf-8', errors='ignore')
        
        soup = BeautifulSoup(html, 'html.parser')
        
        # Remove script and style tags
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Convert HTML headings to markdown
        # This preserves structure for metadata extraction
        for level in range(1, 7):  # h1 through h6
            for heading in soup.find_all(f'h{level}'):
                # Convert <h1>Text</h1> to # Text
                heading_text = heading.get_text().strip()
                if heading_text:
                    markdown_heading = f"{'#' * level} {heading_text}"
                    heading.replace_with(f"\n\n{markdown_heading}\n\n")
        
        # Get text
        text = soup.get_text()
        
        # Clean up excessive whitespace while preserving structure
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
            elif lines and lines[-1]:  # Preserve paragraph breaks
                lines.append('')
        
        cleaned_text = '\n'.join(lines)
        
        return [cleaned_text] if cleaned_text else [""]


def get_extractor(mime_type: str) -> TextExtractor:
    """
    Factory function to get appropriate extractor for MIME type.
    
    Args:
        mime_type: MIME type string
    
    Returns:
        Appropriate text extractor
    
    Raises:
        ValueError: If MIME type is not supported
    """
    extractors = {
        'application/pdf': PDFExtractor(),
        'text/plain': TextFileExtractor(),
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': DocxExtractor(),
        'text/html': HTMLExtractor(),
        'application/msword': DocxExtractor(),  # Older .doc files (best effort)
    }
    
    if mime_type not in extractors:
        raise ValueError(f"Unsupported MIME type: {mime_type}")
    
    return extractors[mime_type]
