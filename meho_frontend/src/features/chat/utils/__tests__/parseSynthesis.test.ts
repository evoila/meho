// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { describe, it, expect } from 'vitest';
import { stripSynthesisXml, parseSynthesis, processCitations } from '../parseSynthesis';

describe('stripSynthesisXml', () => {
  it('removes <follow_ups> blocks entirely', () => {
    const input = `Here is the answer.\n<follow_ups>\n<question>What about X?</question>\n<question>And Y?</question>\n</follow_ups>`;
    const result = stripSynthesisXml(input);
    expect(result).toBe('Here is the answer.');
    expect(result).not.toContain('follow_ups');
    expect(result).not.toContain('<question>');
  });

  it('converts <hypotheses> block to markdown list', () => {
    const input = `Some context.\n<hypotheses>\n<hypothesis status="validated">Cluster is healthy</hypothesis>\n<hypothesis status="invalidated">Node is down</hypothesis>\n<hypothesis status="inconclusive">Memory issue</hypothesis>\n</hypotheses>`;
    const result = stripSynthesisXml(input);
    expect(result).toContain('### Key Findings');
    expect(result).toContain('**\u2713 Validated:** Cluster is healthy');
    expect(result).toContain('**\u2717 Invalidated:** Node is down');
    expect(result).toContain('**? Inconclusive:** Memory issue');
    expect(result).not.toContain('<hypotheses>');
    expect(result).not.toContain('<hypothesis');
  });

  it('strips inline <hypothesis> tags from specialist output', () => {
    const input = `The pod is crashing. <hypothesis id="h-1" status="validated">OOMKill due to memory leak</hypothesis> confirmed by restart count.`;
    const result = stripSynthesisXml(input);
    expect(result).toContain('**\u2713 Validated:** OOMKill due to memory leak');
    expect(result).not.toContain('<hypothesis');
  });

  it('removes <hypothesis_tracking> blocks', () => {
    const input = `Answer here.\n<hypothesis_tracking>\nSome internal tracking data\n</hypothesis_tracking>\nMore text.`;
    const result = stripSynthesisXml(input);
    expect(result).not.toContain('hypothesis_tracking');
    expect(result).toContain('Answer here.');
    expect(result).toContain('More text.');
  });

  it('strips <summary> and <reasoning> wrapper tags but keeps content', () => {
    const input = `<summary>Executive summary here.</summary>\n<reasoning>Detailed reasoning.</reasoning>`;
    const result = stripSynthesisXml(input);
    expect(result).toContain('Executive summary here.');
    expect(result).toContain('Detailed reasoning.');
    expect(result).not.toContain('<summary>');
    expect(result).not.toContain('<reasoning>');
  });

  it('converts [connector:Name] markers to headings', () => {
    const input = `[connector:Production K8s]\nPod status is healthy.\n\n[connector:Prometheus]\nMetrics look normal.`;
    const result = stripSynthesisXml(input);
    expect(result).toContain('### Production K8s');
    expect(result).toContain('### Prometheus');
    expect(result).not.toContain('[connector:');
  });

  it('collapses excessive blank lines', () => {
    const input = `Text before.\n\n\n\n\nText after.`;
    const result = stripSynthesisXml(input);
    expect(result).toBe('Text before.\n\nText after.');
  });

  it('passes through plain markdown unchanged', () => {
    const input = `## Status Report\n\nEverything is fine.\n\n| Name | Status |\n|------|--------|\n| pod-1 | Running |`;
    const result = stripSynthesisXml(input);
    expect(result).toBe(input);
  });

  it('handles a full synthesis response with all XML sections', () => {
    const input = [
      '<summary>The cluster is healthy with **94/97 deployments** running.</summary>',
      '<reasoning>',
      '[connector:GKE Cluster]',
      'Checked node status: all 5 nodes Ready.',
      '',
      '[connector:Prometheus]',
      'CPU usage normal across all nodes.',
      '</reasoning>',
      '<hypotheses>',
      '<hypothesis status="validated">Cluster is operationally healthy.</hypothesis>',
      '</hypotheses>',
      '<follow_ups>',
      '<question>Should I check pod logs?</question>',
      '</follow_ups>',
    ].join('\n');

    const result = stripSynthesisXml(input);
    expect(result).toContain('The cluster is healthy with **94/97 deployments** running.');
    expect(result).toContain('### GKE Cluster');
    expect(result).toContain('### Prometheus');
    expect(result).toContain('### Key Findings');
    expect(result).toContain('**\u2713 Validated:** Cluster is operationally healthy.');
    expect(result).not.toContain('<follow_ups>');
    expect(result).not.toContain('<summary>');
    expect(result).not.toContain('<reasoning>');
  });
});

describe('parseSynthesis', () => {
  it('returns null when no <summary> tag is found', () => {
    expect(parseSynthesis('Just a plain message.')).toBeNull();
  });

  it('parses structured synthesis with all sections', () => {
    const input = [
      '<summary>Cluster is healthy.</summary>',
      '<reasoning>[connector:K8s]All pods running.</reasoning>',
      '<hypotheses><hypothesis status="validated">All good</hypothesis></hypotheses>',
    ].join('\n');
    const result = parseSynthesis(input);
    expect(result).not.toBeNull();
    if (!result) throw new Error('Expected non-null result');
    expect(result.summary).toBe('Cluster is healthy.');
    expect(result.hypotheses).toHaveLength(1);
    expect(result.hypotheses[0].status).toBe('validated');
    expect(result.connectorSegments).toHaveLength(1);
    expect(result.connectorSegments[0].connectorName).toBe('K8s');
  });
});

describe('processCitations', () => {
  it('replaces [src:step-N] with [^N] superscripts', () => {
    const { processed, stepMap } = processCitations('Found issue [src:step-3] and related [src:step-5].');
    expect(processed).toBe('Found issue [^1] and related [^2].');
    expect(stepMap['1']).toBe('step-3');
    expect(stepMap['2']).toBe('step-5');
  });

  it('deduplicates repeated step references', () => {
    const { processed } = processCitations('Issue [src:step-1] is confirmed [src:step-1].');
    expect(processed).toBe('Issue [^1] is confirmed [^1].');
  });
});
