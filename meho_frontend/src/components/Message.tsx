// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Chat Message Components
 *
 * Renders chat messages with markdown support, syntax-highlighted code blocks,
 * and one-click copy functionality. Uses rehype-highlight for code highlighting
 * with the atom-one-dark theme.
 */
import { useState, useCallback, type ReactNode } from 'react';
import { User, Loader2, Copy, Check } from 'lucide-react';
import { motion } from 'motion/react';
import clsx from 'clsx';
import mehoAvatar from '../assets/meho-avatar.svg';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { common } from 'lowlight';
import dockerfile from 'highlight.js/lib/languages/dockerfile';
import nginx from 'highlight.js/lib/languages/nginx';
// Atom One Dark theme for syntax highlighting (matches dark UI)
import 'highlight.js/styles/atom-one-dark.css';

/** Additional DevOps languages beyond the common bundle (37 languages).
 *  YAML, JSON, Bash, Python, SQL, XML, Go, Java, JS, TS are all in common. */
const languages = { ...common, dockerfile, nginx };

/**
 * Code block wrapper with language label header and copy-to-clipboard button.
 * Wraps <pre> elements produced by react-markdown + rehype-highlight.
 */
function CodeBlockWrapper({ children, ...props }: Readonly<{ children?: ReactNode; [key: string]: unknown }>) {
  const [copied, setCopied] = useState(false);

  // Extract plain text from the code element tree for clipboard
  const getCodeText = useCallback((): string => {
    const codeElement = (children as React.ReactElement<{ children?: React.ReactNode; className?: string }>)?.props;
    if (!codeElement) return '';
    const extractText = (node: React.ReactNode): string => {
      if (typeof node === 'string') return node;
      if (Array.isArray(node)) return node.map(extractText).join('');
      if (node && typeof node === 'object' && 'props' in node) {
        return extractText((node as React.ReactElement<{ children?: React.ReactNode }>).props.children);
      }
      return '';
    };
    return extractText(codeElement.children);
  }, [children]);

  // Extract language from className (set by rehype-highlight as "language-xxx")
  const language = (() => {
    const className = (children as React.ReactElement<{ className?: string }>)?.props?.className ?? '';
    const match = /language-(\w+)/.exec(className);
    return match ? match[1] : 'code';
  })();

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(getCodeText());
    } catch {
      // Fallback for non-secure contexts
      const textarea = document.createElement('textarea');
      textarea.value = getCodeText();
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="relative my-4 rounded-lg overflow-hidden border border-white/10 bg-[#0d0d0d]">
      <div className="flex items-center justify-between px-4 py-2 bg-white/5 border-b border-white/5">
        <span className="text-xs font-mono text-text-tertiary lowercase">
          {language}
        </span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1.5 text-xs text-text-tertiary hover:text-white transition-colors"
          type="button"
        >
          {copied ? (
            <>
              <Check className="h-3.5 w-3.5 text-green-400" />
              <span className="text-green-400">Copied</span>
            </>
          ) : (
            <>
              <Copy className="h-3.5 w-3.5" />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <pre className="p-4 overflow-x-auto !m-0 !bg-transparent" {...props}>
        {children}
      </pre>
    </div>
  );
}

export interface MessageProps {
  role: 'user' | 'assistant';
  content: string;
  isStreaming?: boolean;
  isProgressUpdate?: boolean;
  /** War room sender attribution (Phase 39) */
  senderName?: string;
  /** Whether to show the sender name label (false when consecutive from same sender) */
  showSenderName?: boolean;
}

export function Message({ role, content, isStreaming, isProgressUpdate, senderName, showSenderName }: Readonly<MessageProps>) {
  const isUser = role === 'user';

  // Progress updates: Cursor IDE style (grayed out, minimal, no bubble)
  if (isProgressUpdate) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 5 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex gap-3 mb-2 items-center px-4"
      >
        <div className="flex-shrink-0 w-8" /> {/* Spacer for alignment */}
        <div className="flex-1">
          <div className="text-xs text-text-tertiary italic flex items-center gap-2">
            <div className="w-1 h-1 rounded-full bg-text-tertiary" />
            {content}
          </div>
        </div>
      </motion.div>
    );
  }

  // Regular messages
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      className={clsx(
        "flex gap-4 mb-6 px-4 group",
        isUser ? "flex-row-reverse" : ""
      )}
    >
      {/* Avatar */}
      {isUser ? (
        <div className="flex-shrink-0 h-9 w-9 rounded-xl flex items-center justify-center shadow-lg transition-transform group-hover:scale-105 bg-gradient-to-br from-primary to-accent">
          <User className="h-5 w-5 text-white" />
        </div>
      ) : (
        <img 
          src={mehoAvatar} 
          alt="MEHO" 
          className="flex-shrink-0 h-9 w-9 rounded-full shadow-lg transition-transform group-hover:scale-105"
        />
      )}

      {/* Message content */}
      <div className={clsx("flex-1 max-w-3xl min-w-0", isUser ? "text-right" : "")}>
        {/* War room sender name label (Phase 39) */}
        {isUser && showSenderName && senderName && (
          <div className="text-[11px] text-text-tertiary font-medium mb-1 text-right" data-testid="sender-name-label">
            {senderName}
          </div>
        )}
        <div className={clsx(
          "inline-block text-left px-5 py-3.5 rounded-2xl shadow-sm transition-all overflow-hidden max-w-full",
          isUser
            ? "bg-primary text-white rounded-tr-sm"
            : "bg-surface border border-border text-text-primary rounded-tl-sm hover:border-primary/30"
        )}>
          <div className={clsx(
            "prose prose-sm max-w-none break-words leading-relaxed",
            isUser ? "prose-invert" : "prose-invert" // Always dark mode style for now as we are in dark mode
          )}>
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[[rehypeHighlight, { detect: false, languages }]]}
              components={{
                // Code block wrapper: header bar with language label + copy button
                pre: (props) => <CodeBlockWrapper {...props} />,
                // Inline code styling (block code handled by pre override)
                code({ className, children, ...props }) {
                  const isInline = !className?.includes('language-') && !className?.includes('hljs');
                  if (isInline) {
                    return (
                      <code className="bg-black/20 px-1.5 py-0.5 rounded text-accent font-mono text-[0.9em]" {...props}>
                        {children}
                      </code>
                    );
                  }
                  // Block code: rehype-highlight handles styling, just pass through
                  return <code className={clsx("font-mono text-sm", className)} {...props}>{children}</code>;
                },
                // Custom link styling -- content provided via react-markdown spread props
                a: ({ node: _node, ...props }) => (
                  // eslint-disable-next-line jsx-a11y/anchor-has-content
                  <a
                    className="text-accent hover:text-accent-hover underline decoration-accent/30 hover:decoration-accent transition-colors"
                    target="_blank"
                    rel="noopener noreferrer"
                    {...props}
                  />
                ),
                // Custom list styling
                ul: ({ node: _node, ...props }) => <ul className="list-disc pl-4 my-2 space-y-1 marker:text-text-tertiary" {...props} />,
                ol: ({ node: _node, ...props }) => <ol className="list-decimal pl-4 my-2 space-y-1 marker:text-text-tertiary" {...props} />,
                // Custom heading styling -- content provided via react-markdown spread props
                // eslint-disable-next-line jsx-a11y/heading-has-content
                h1: ({ node: _node, ...props }) => <h1 className="text-xl font-bold mb-3 mt-4 text-white" {...props} />,
                // eslint-disable-next-line jsx-a11y/heading-has-content
                h2: ({ node: _node, ...props }) => <h2 className="text-lg font-bold mb-2 mt-3 text-white" {...props} />,
                // eslint-disable-next-line jsx-a11y/heading-has-content
                h3: ({ node: _node, ...props }) => <h3 className="text-base font-semibold mb-2 mt-3 text-white" {...props} />,
                // Custom table styling
                table: ({ node: _node, ...props }) => (
                  <div className="overflow-x-auto my-4 rounded-lg border border-white/10">
                    <table className="min-w-full divide-y divide-white/10" {...props} />
                  </div>
                ),
                thead: ({ node: _node, ...props }) => <thead className="bg-white/5" {...props} />,
                th: ({ node: _node, ...props }) => <th className="px-4 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider" {...props} />,
                td: ({ node: _node, ...props }) => <td className="px-4 py-3 text-sm text-text-tertiary border-t border-white/5" {...props} />,
                blockquote: ({ node: _node, ...props }) => <blockquote className="border-l-4 border-primary/30 pl-4 italic text-text-secondary my-4" {...props} />,
              }}
            >
              {content}
            </ReactMarkdown>
          </div>
          {isStreaming && (
            <div className="flex items-center gap-2 mt-2 text-sm opacity-70">
              <Loader2 className="h-3 w-3 animate-spin" />
              <span className="text-xs font-medium">Generating...</span>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}

export function TypingIndicator() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex gap-4 mb-6 px-4"
      data-testid="typing-indicator"
    >
      <img 
        src={mehoAvatar} 
        alt="MEHO" 
        className="flex-shrink-0 h-9 w-9 rounded-full shadow-lg animate-pulse"
      />
      <div className="flex-1">
        <div className="inline-block px-5 py-3.5 rounded-2xl bg-surface/50 border border-border rounded-tl-sm">
          <div className="flex items-center gap-2 text-text-secondary">
            <Loader2 className="h-4 w-4 animate-spin text-accent" />
            <span className="text-sm font-medium">Thinking...</span>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

