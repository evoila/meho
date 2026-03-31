"""
Text chunking for knowledge ingestion.

Splits text into manageable chunks with overlap for context.
Intelligently handles code examples and JSON to keep them intact.
"""
# mypy: disable-error-code="var-annotated"
from typing import List, Tuple, Dict, Any
import tiktoken
import re


class TextChunker:
    """Chunk text into pieces suitable for embedding"""
    
    def __init__(
        self,
        max_tokens: int = 512,
        overlap_tokens: int = 50,
        encoding_name: str = "cl100k_base"  # GPT-3.5/4 encoding
    ):
        """
        Initialize text chunker.
        
        Args:
            max_tokens: Maximum tokens per chunk
            overlap_tokens: Number of overlapping tokens between chunks
            encoding_name: Tiktoken encoding name
        """
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.encoding = tiktoken.get_encoding(encoding_name)
    
    def chunk_text(self, text: str) -> List[str]:
        """
        Chunk text into pieces of max_tokens with overlap.
        
        Intelligently handles code examples:
        - Keeps JSON blocks intact when possible
        - Avoids breaking in the middle of code examples
        - Preserves context around code snippets
        - Tries to split on sentence boundaries for regular text
        
        Args:
            text: Text to chunk
        
        Returns:
            List of text chunks
        """
        if not text or not text.strip():
            return []
        
        # Tokenize the full text
        tokens = self.encoding.encode(text)
        
        # If text is short enough, return as-is
        if len(tokens) <= self.max_tokens:
            return [text]
        
        # Identify code blocks and JSON examples for special handling
        code_blocks = self._find_code_blocks(text)
        
        chunks = []
        start = 0
        
        while start < len(tokens):
            # Get chunk of tokens
            end = min(start + self.max_tokens, len(tokens))
            chunk_tokens = tokens[start:end]
            chunk_text = self.encoding.decode(chunk_tokens)
            
            # Try to find a good break point if not the last chunk
            if end < len(tokens):
                chunk_text = self._find_break_point(
                    chunk_text, code_blocks, tokens[start:end]
                )
                # Re-tokenize to get actual token count
                chunk_tokens = self.encoding.encode(chunk_text)
            
            chunks.append(chunk_text.strip())
            
            # Move start forward, accounting for overlap
            # Ensure we always advance by at least 1 token to prevent infinite loops
            advance = len(chunk_tokens) - self.overlap_tokens
            if advance < 1:
                # If chunk is shorter than overlap (common for last chunk),
                # advance by the full chunk length to avoid infinite loop
                advance = len(chunk_tokens)
            
            start += advance
            
            # Safety check: prevent infinite loop
            if advance == 0:
                break
        
        return [c for c in chunks if c]  # Filter out empty chunks
    
    def _find_code_blocks(self, text: str) -> List[Tuple[int, int]]:
        """
        Find all code blocks and JSON examples in text.
        
        Returns list of (start_pos, end_pos) tuples for code regions.
        """
        code_regions = []
        
        # Pattern 1: JSON with "elements" array (common API response)
        elements_pattern = re.compile(r'\{\s*"elements"\s*:\s*\[[\s\S]+?\]\s*\}')
        for match in elements_pattern.finditer(text):
            code_regions.append((match.start(), match.end()))
        
        # Pattern 2: Markdown code blocks
        code_block_pattern = re.compile(r'```[\s\S]+?```')
        for match in code_block_pattern.finditer(text):
            code_regions.append((match.start(), match.end()))
        
        # Pattern 3: Large JSON objects (likely examples)
        # Must have multiple fields to avoid matching random JSON
        json_pattern = re.compile(
            r'\{(?:[^{}]|\{[^{}]*\})*"(?:id|name|type|status|elements)"[\s\S]{30,}?\}',
            re.IGNORECASE
        )
        for match in json_pattern.finditer(text):
            # Only if not already covered by other patterns
            start, end = match.start(), match.end()
            if not any(cs <= start and end <= ce for cs, ce in code_regions):
                code_regions.append((start, end))
        
        return sorted(code_regions)
    
    def _find_break_point(
        self, chunk_text: str, code_blocks: List[Tuple[int, int]], tokens: List[int]
    ) -> str:
        """
        Find the best break point for a chunk, avoiding breaking code blocks.
        
        Args:
            chunk_text: The chunk text to find break point in
            code_blocks: List of (start, end) positions of code in full text (not used in current impl)
            tokens: Token list for this chunk
        
        Returns:
            Adjusted chunk text ending at a good break point
        """
        chunk_len = len(chunk_text)
        
        # Strategy: Scan backward from end to find first safe break point
        # Check progressively larger portions to handle code blocks of any size
        
        # First, try to detect if we have code anywhere in the last half of the chunk
        # This handles code blocks larger than 200 chars
        last_half_start = max(0, chunk_len // 2)
        if self._looks_like_code(chunk_text[last_half_start:]):
            # We have code in the latter part of the chunk
            # Search backward from the middle to find a break before the code starts
            # Search up to 80% of chunk length to handle large code blocks
            search_limit = max(0, int(chunk_len * 0.2))
            
            for i in range(last_half_start, search_limit, -1):
                if i < len(chunk_text):
                    # Look for strong break indicators (paragraph breaks)
                    if i > 0 and chunk_text[i-1:i+1] == '\n\n':
                        return chunk_text[:i+1]
                    # Or strong sentence boundaries not in JSON
                    if chunk_text[i] in '.!?' and not self._in_json_context(chunk_text, i):
                        return chunk_text[:i+1]
            
            # If we couldn't find a good break, try to at least break on a newline
            for i in range(last_half_start, search_limit, -1):
                if i < len(chunk_text) and chunk_text[i] == '\n':
                    if not self._in_json_context(chunk_text, i):
                        return chunk_text[:i+1]
        
        # Standard sentence boundary breaking (for non-code text)
        # Search backward from end, looking for good break points
        for i in range(chunk_len - 1, max(0, chunk_len - 200), -1):
            if i < len(chunk_text):
                char = chunk_text[i]
                # Prefer double newlines (paragraph breaks) over single
                if i > 0 and chunk_text[i-1:i+1] == '\n\n':
                    return chunk_text[:i+1]
                elif char in '.!?':
                    # Make sure we're not in a JSON context
                    if not self._in_json_context(chunk_text, i):
                        return chunk_text[:i+1]
                elif char == '\n' and not self._in_json_context(chunk_text, i):
                    return chunk_text[:i+1]
        
        # Last resort: return full chunk if we can't find any good break point
        # This is safer than breaking in the middle of code
        return chunk_text
    
    @staticmethod
    def _looks_like_code(text: str) -> bool:
        """Check if text segment looks like code/JSON."""
        if not text:
            return False
        
        # JSON indicators
        json_chars = text.count('{') + text.count('}') + text.count('[') + text.count(']')
        if json_chars > 4 and ':' in text:
            return True
        
        # Code block markers
        if '```' in text:
            return True
        
        # Indented code (multiple lines starting with spaces)
        lines = text.split('\n')
        indented = sum(1 for line in lines if line.startswith('  ') or line.startswith('\t'))
        if indented > 3:
            return True
        
        return False
    
    @staticmethod
    def _in_json_context(text: str, pos: int) -> bool:
        """Check if position is inside a JSON structure."""
        # Count braces/brackets before position
        before = text[:pos]
        open_braces = before.count('{') - before.count('}')
        open_brackets = before.count('[') - before.count(']')
        
        # If we have unclosed braces/brackets, we're likely in JSON
        return open_braces > 0 or open_brackets > 0
    
    def chunk_pages(self, pages: List[str]) -> List[Tuple[str, int]]:
        """
        Chunk a list of pages (e.g., from PDF).
        
        Args:
            pages: List of page texts
        
        Returns:
            List of (chunk_text, page_number) tuples
        """
        chunks_with_pages = []
        
        for page_num, page_text in enumerate(pages, start=1):
            if not page_text or not page_text.strip():
                continue
            
            page_chunks = self.chunk_text(page_text)
            for chunk in page_chunks:
                chunks_with_pages.append((chunk, page_num))
        
        return chunks_with_pages
    
    def chunk_document_with_structure(
        self,
        text: str,
        document_name: str,
        detect_headings: bool = True
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """
        Chunk document and return chunks with structural context.
        
        Tracks document structure (headings) to enable metadata extraction.
        
        Args:
            text: Document text to chunk
            document_name: Name of source document
            detect_headings: Whether to parse and track heading hierarchy
        
        Returns:
            List of (chunk_text, context_dict) tuples where context includes:
            - heading_stack: List of heading hierarchy
            - document_name: Source document name
        """
        chunks_with_context = []
        
        if detect_headings:
            # Parse document structure first
            sections = self._parse_document_structure(text)
            
            # Chunk each section with its context
            for section_text, heading_stack in sections:
                section_chunks = self.chunk_text(section_text)
                for chunk in section_chunks:
                    context = {
                        "heading_stack": heading_stack.copy(),
                        "document_name": document_name
                    }
                    chunks_with_context.append((chunk, context))
        else:
            # Fallback to simple chunking
            simple_chunks = self.chunk_text(text)
            for chunk in simple_chunks:
                context = {
                    "document_name": document_name,
                    "heading_stack": []
                }
                chunks_with_context.append((chunk, context))
        
        return chunks_with_context
    
    def _parse_document_structure(self, text: str) -> List[Tuple[str, List[str]]]:
        """
        Parse document into sections with heading hierarchy.
        
        Detects markdown headings and tracks the hierarchy.
        
        Args:
            text: Document text
        
        Returns:
            List of (section_text, heading_stack) tuples
        """
        sections = []
        current_heading_stack = []
        current_text = []
        
        lines = text.split('\n')
        
        for line in lines:
            # Detect markdown headings (# Header, ## Subheader, etc.)
            heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            
            if heading_match:
                # Save previous section
                if current_text:
                    section_text = '\n'.join(current_text)
                    sections.append((section_text, current_heading_stack.copy()))
                    current_text = []
                
                # Update heading stack
                level = len(heading_match.group(1))
                heading_text = heading_match.group(2).strip()
                
                # Trim stack to appropriate level
                current_heading_stack = current_heading_stack[:level-1]
                current_heading_stack.append(heading_text)
            else:
                current_text.append(line)
        
        # Add final section
        if current_text:
            section_text = '\n'.join(current_text)
            sections.append((section_text, current_heading_stack.copy()))
        
        return sections


def chunk_text(
    text: str,
    max_tokens: int = 512,
    overlap_tokens: int = 50,
    encoding_name: str = "cl100k_base",
) -> List[str]:
    """
    Convenience helper that mirrors the historic chunk_text() function.

    Older ingestion paths import chunk_text directly, so we keep this
    wrapper to avoid import errors while internally delegating to the
    TextChunker implementation.

    Args:
        text: Text to split into chunks.
        max_tokens: Maximum tokens per chunk.
        overlap_tokens: Number of tokens overlapped between chunks.
        encoding_name: Tiktoken encoding to use.

    Returns:
        List of chunk strings.
    """
    chunker = TextChunker(
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        encoding_name=encoding_name,
    )
    return chunker.chunk_text(text)
