// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * SkillEditor Component
 *
 * Two-tab editor for orchestrator skill content:
 * - Edit tab: name, description, and markdown textarea with monospace font
 * - Preview tab: ReactMarkdown + remarkGfm rendering
 *
 * Follows the same markdown rendering style as the connector SkillEditor.
 *
 * Phase 52 - Orchestrator Skills Frontend
 */

import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Save, X, Eye, Edit3 } from 'lucide-react';
import clsx from 'clsx';
import type { OrchestratorSkill } from '../../api/orchestratorSkills';

interface SkillEditorProps {
  skill: OrchestratorSkill | null;
  isNew: boolean;
  onSave: (data: { name: string; description: string; content: string }) => void;
  onCancel: () => void;
  saving?: boolean;
}

type EditorTab = 'edit' | 'preview';

export function SkillEditor({
  skill,
  isNew,
  onSave,
  onCancel,
  saving = false,
}: Readonly<SkillEditorProps>) {
  const [tab, setTab] = useState<EditorTab>('edit');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [content, setContent] = useState('');

  // Reset form when skill/isNew changes -- "adjusting state during render"
  // pattern per React docs (avoids cascading re-render from useEffect+setState).
  const [prevSkill, setPrevSkill] = useState<OrchestratorSkill | null | undefined>(undefined);
  const [prevIsNew, setPrevIsNew] = useState<boolean | undefined>(undefined);
  if (prevSkill !== skill || prevIsNew !== isNew) {
    setPrevSkill(skill);
    setPrevIsNew(isNew);
    if (skill) {
      setName(skill.name);
      setDescription(skill.description || '');
      setContent(skill.content);
    } else if (isNew) {
      setName('');
      setDescription('');
      setContent('');
    }
    setTab('edit');
  }

  const handleSave = () => {
    if (!name.trim()) return;
    if (!content.trim()) return;
    onSave({ name: name.trim(), description: description.trim(), content: content.trim() });
  };

  const canSave = name.trim().length > 0 && content.trim().length > 0;

  return (
    <div className="flex flex-col h-full">
      {/* Header with name and description inputs */}
      <div className="space-y-3 mb-4">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Skill name"
          className="w-full px-4 py-2.5 bg-white/5 border border-white/10 rounded-xl text-white placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-sm font-medium"
        />
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Short description (optional)"
          className="w-full px-4 py-2 bg-white/5 border border-white/10 rounded-xl text-white placeholder-zinc-500 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all text-sm"
        />
      </div>

      {/* Tab bar */}
      <div className="flex items-center gap-1 mb-3 border-b border-white/10 pb-2">
        <button
          onClick={() => setTab('edit')}
          className={clsx(
            'flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg transition-colors',
            tab === 'edit'
              ? 'text-white bg-white/10'
              : 'text-zinc-400 hover:text-white hover:bg-white/5'
          )}
        >
          <Edit3 className="h-3.5 w-3.5" />
          Edit
        </button>
        <button
          onClick={() => setTab('preview')}
          className={clsx(
            'flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg transition-colors',
            tab === 'preview'
              ? 'text-white bg-white/10'
              : 'text-zinc-400 hover:text-white hover:bg-white/5'
          )}
        >
          <Eye className="h-3.5 w-3.5" />
          Preview
        </button>
      </div>

      {/* Content area */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {tab === 'edit' ? (
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            placeholder="Enter skill content in markdown...&#10;&#10;Describe investigation patterns, cross-system diagnosis steps, or operational procedures."
            className="w-full h-full min-h-[400px] px-4 py-3 bg-zinc-900/80 border border-white/10 rounded-xl text-white placeholder-zinc-600 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary/50 transition-all font-mono text-sm resize-y leading-relaxed"
            rows={20}
          />
        ) : (
          <div className="px-4 py-3 bg-zinc-900/40 border border-white/10 rounded-xl min-h-[400px]">
            {content ? (
              <div className="prose prose-sm max-w-none prose-invert">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    code({ className, children, ...props }) {
                      const match = /language-(\w+)/.exec(className || '');
                      const isInline = !match && !String(children).includes('\n');

                      return isInline ? (
                        <code className="bg-black/20 px-1.5 py-0.5 rounded text-accent font-mono text-[0.9em]" {...props}>
                          {children}
                        </code>
                      ) : (
                        <div className="relative group/code my-4 rounded-lg overflow-hidden border border-white/10 bg-[#0d0d0d]">
                          <div className="flex items-center justify-between px-4 py-2 bg-white/5 border-b border-white/5">
                            <span className="text-xs font-mono text-zinc-500 lowercase">
                              {match ? match[1] : 'code'}
                            </span>
                          </div>
                          <div className="p-4 overflow-x-auto">
                            <code className={clsx('font-mono text-sm', className)} {...props}>
                              {children}
                            </code>
                          </div>
                        </div>
                      );
                    },
                    a: ({ ...props }) => (
                      // eslint-disable-next-line jsx-a11y/anchor-has-content -- content provided via react-markdown spread props
                      <a
                        className="text-accent hover:text-accent-hover underline decoration-accent/30 hover:decoration-accent transition-colors"
                        target="_blank"
                        rel="noopener noreferrer"
                        {...props}
                      />
                    ),
                    ul: ({ ...props }) => <ul className="list-disc pl-4 my-2 space-y-1 marker:text-zinc-500" {...props} />,
                    ol: ({ ...props }) => <ol className="list-decimal pl-4 my-2 space-y-1 marker:text-zinc-500" {...props} />,
                    // eslint-disable-next-line jsx-a11y/heading-has-content
                    h1: ({ ...props }) => <h1 className="text-xl font-bold mb-3 mt-4 text-white" {...props} />,
                    // eslint-disable-next-line jsx-a11y/heading-has-content
                    h2: ({ ...props }) => <h2 className="text-lg font-bold mb-2 mt-3 text-white" {...props} />,
                    // eslint-disable-next-line jsx-a11y/heading-has-content
                    h3: ({ ...props }) => <h3 className="text-base font-semibold mb-2 mt-3 text-white" {...props} />,
                    table: ({ ...props }) => (
                      <div className="overflow-x-auto my-4 rounded-lg border border-white/10">
                        <table className="min-w-full divide-y divide-white/10" {...props} />
                      </div>
                    ),
                    thead: ({ ...props }) => <thead className="bg-white/5" {...props} />,
                    th: ({ ...props }) => <th className="px-4 py-3 text-left text-xs font-medium text-zinc-400 uppercase tracking-wider" {...props} />,
                    td: ({ ...props }) => <td className="px-4 py-3 whitespace-nowrap text-sm text-zinc-400 border-t border-white/5" {...props} />,
                    blockquote: ({ ...props }) => <blockquote className="border-l-4 border-primary/30 pl-4 italic text-zinc-400 my-4" {...props} />,
                  }}
                >
                  {content}
                </ReactMarkdown>
              </div>
            ) : (
              <p className="text-zinc-500 text-sm italic">Nothing to preview yet.</p>
            )}
          </div>
        )}
      </div>

      {/* Action buttons */}
      <div className="flex gap-3 mt-4 pt-4 border-t border-white/10">
        <button
          onClick={handleSave}
          disabled={!canSave || saving}
          className="flex items-center gap-2 px-6 py-2.5 bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 text-white rounded-xl font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? (
            <>
              <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white" />
              Saving...
            </>
          ) : (
            <>
              <Save className="h-4 w-4" />
              {isNew ? 'Create Skill' : 'Save Changes'}
            </>
          )}
        </button>
        <button
          onClick={onCancel}
          disabled={saving}
          className="flex items-center gap-2 px-6 py-2.5 bg-white/5 hover:bg-white/10 border border-white/10 rounded-xl text-white transition-all disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <X className="h-4 w-4" />
          Cancel
        </button>
      </div>
    </div>
  );
}
