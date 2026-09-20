/** Shared review-queue fixtures (a support module, NOT a test file: importing a
 * test file would run its tests inside the importing file). */

export function reviewItem(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    claim: {
      claim_id: 'clm_disputed',
      statement: 'mAP 提升 2.1 个百分点。',
      claim_type: 'FACT',
      support_state: 'DISPUTED',
      paper_version_id: 'pver_1',
      evidence_count: 2,
      is_superseded: false,
    },
    paper_id: 'pap_1',
    paper_version_id: 'pver_1',
    paper_title: 'Detection study',
    group: 'CONTRADICTION',
    reasons: ['verification.contradiction → FAIL: contradicts claim clm_other'],
    impact: 'HIGH',
    source_refs: [],
    original_evidence: [
      {
        paper_id: 'pap_1',
        paper_version_id: 'pver_1',
        claim_id: 'clm_disputed',
        evidence_ids: ['ev_a'],
      },
    ],
    counter_evidence: [
      { paper_id: 'pap_1', paper_version_id: 'pver_1', claim_id: 'clm_other', evidence_ids: ['ev_b'] },
    ],
    verifications: [
      {
        verification_id: 'ver_1',
        verifier_type: 'verification.contradiction',
        status: 'FAIL',
        verdict: 'FAIL',
        reason_summary: 'contradicts claim clm_other',
        run_id: 'run_old',
      },
    ],
    claim_revision: 'rev-abc',
    personal_decision: null,
    ...overrides,
  };
}
