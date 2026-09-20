/** UI contract 1.0.0. Design definitions, not a generated live OpenAPI client. */
export type Id = string; // Opaque. Server retains frozen prefixed-ULID validation.
export type ISODateTime = string;
export type MissingReason = 'NOT_REPORTED'|'NOT_EXTRACTED'|'NOT_MEASURED'|'NOT_APPLICABLE'|'UNAVAILABLE'|'UNKNOWN';
export type SupportState = 'UNVERIFIED'|'SUPPORTED'|'PARTIALLY_SUPPORTED'|'DISPUTED'|'UNSUPPORTED'|'RETRACTED'|'INSUFFICIENT_EVIDENCE';
export type ClaimType = 'FACT'|'INFERENCE'|'CRITIQUE'|'EXTERNAL';
export type TaskState = 'PENDING'|'QUEUED'|'RUNNING'|'WAITING'|'RETRYING'|'SUCCEEDED'|'SUCCEEDED_WITH_WARNINGS'|'BLOCKED'|'FAILED'|'CANCELLED'|'SKIPPED';
export type ReadState = 'UNREAD'|'READING'|'READ'|'LATER';
export type Tier = 'T0_INDEX'|'T1_SCAN'|'T2_FULL'|'T3_DEEP';
export interface SourceRef { paper_id:Id; paper_version_id:Id; claim_id:Id|null; evidence_ids:Id[]; }
export interface FactValue<T> { value:T|null; missing_reason:MissingReason|null; source_refs:SourceRef[]; }
export interface Meta { contract_version:'1.0.0'; snapshot_id:Id; observed_at:ISODateTime; partial:boolean; warnings:{code:string;message:string}[]; }
export interface Envelope<T> { data:T; meta:Meta; }
export interface Page<T> {items:T[];next_cursor:string|null;has_more:boolean;total:number|null;total_kind:'EXACT'|'ESTIMATE'|'UNKNOWN';}
export interface Capability {code:string;availability:'AVAILABLE'|'UNAVAILABLE'|'DEGRADED';reason:string|null;}
export interface Capabilities {ui_contract_version:'1.0.0';capabilities:Capability[];limits:{upload_file_bytes:number;upload_batch_bytes:number;upload_files:number;library_page_size:number};mode:'LIVE'|'TEST';}
export interface PersonalState { saved:boolean;read_state:ReadState;revision:number;reading_anchor:{paper_version_id:Id;page_number:number|null;section_id:Id|null;scroll_offset:number|null}|null; }
export interface ClaimPreview {claim_id:Id;statement:string;claim_type:ClaimType;support_state:SupportState;paper_version_id:Id;evidence_count:number;is_superseded:boolean;}
export interface PaperListItem {paper_id:Id;paper_version_id:Id;title:string;authors:FactValue<string[]>;year:FactValue<number>;venue:FactValue<string>;takeaway:ClaimPreview|null;tags:{id:Id;label:string;namespace:string;is_candidate:boolean}[];tier:Tier|null;personal:PersonalState;audit:{total:number;by_state:Partial<Record<SupportState,number>>};}
export interface LibraryQuery {query:string;kind:'PAPERS'|'CLAIMS'|'TECHNIQUES';filters:{collection_ids?:Id[];tag_ids?:Id[];read_states?:ReadState[];years?:number[];venue_ids?:Id[];author_ids?:Id[];tiers?:Tier[];audit_states?:SupportState[]};sort:'RELEVANCE'|'RECENT'|'TITLE';cursor:string|null;limit:number;}
export interface ModuleView {id:string;label:string;availability:Capability['availability'];reason:string|null;claims:ClaimPreview[];}
export interface Workspace {paper:PaperListItem;document_sha256:string;selected_version_id:Id;modules:ModuleView[];versions:{id:Id;label:string;is_latest:boolean}[];analysis_revision:string;}
export interface EvidenceLocator {evidence_id:Id;paper_version_id:Id;document_sha256:string;page_number:number;page_label:string|null;coordinate_space:'DISPLAY_NORMALIZED_V1';rect_norm:[number,number,number,number]|null;precision:'REGION'|'PAGE'|'TEXT_ONLY';source_method:string;ocr_confidence:number|null;transform_revision:string;extraction_run_id:Id;reason:string|null;}
export interface PageEvidence {paper_version_id:Id;document_sha256:string;page_number:number;page_display_width:number;page_display_height:number;reference_rotation:0|90|180|270;evidence:EvidenceLocator[];}
export interface Note {note_id:Id;paper_id:Id;paper_version_id:Id;claim_id:Id|null;body:string;revision:number;updated_at:ISODateTime;}
export interface ReviewItem {claim:ClaimPreview;paper_id:Id;paper_title:string;reasons:string[];impact:'HIGH'|'NORMAL'|'LOW';source_refs:SourceRef[];personal_decision:'SEEN'|'NEEDS_REVIEW'|'RESERVATION'|null;}
export interface ReviewDecision {decision_id:Id;claim_id:Id;paper_version_id:Id;decision:'SEEN'|'NEEDS_REVIEW'|'RESERVATION';note:string|null;created_at:ISODateTime;}
export type OperationInput =
 | {kind:'reverify';claim_ids:Id[];model_role:string;expected_analysis_revision:string}
 | {kind:'reanalyze';paper_version_ids:Id[];tier:Tier;model_role:string}
 | {kind:'set_tier';paper_ids:Id[];tier:Tier}
 | {kind:'cancel_job'|'resume_job';job_id:Id}
 | {kind:'replay_task';task_id:Id}
 | {kind:'corpus_refresh';collection_id:Id;selection_hash:string}
 | {kind:'gc_preview'}
 | {kind:'gc_execute';preview_id:Id;scope_hash:string}
 | {kind:'export';paper_version_ids:Id[];format:'JSON'|'MARKDOWN'|'CSV'};
export interface OperationResult {operation_id:Id;job_ids:Id[];state:'ACCEPTED'|'RUNNING'|'COMPLETED'|'PARTIAL'|'FAILED'|'CANCELLED';scope:{paper_version_ids:Id[];claim_ids:Id[]};results:{target_id:Id;status:string;error_code:string|null}[];}
export interface ImportBatch {batch_id:Id;items:{item_id:Id;filename:string;size_bytes:number;state:'PENDING'|'UPLOADING'|'RECEIVED'|'QUEUED'|'IMPORTED'|'DUPLICATE'|'FAILED'|'CANCELLED';paper_id:Id|null;paper_version_id:Id|null;job_id:Id|null;error_code:string|null}[];}
export interface ObservedMetric {value:number|null;unit:string;state:'FRESH'|'STALE'|'UNKNOWN';observed_at:ISODateTime|null;sample_count:number;window_seconds:number|null;reason:string|null;}
export interface OperationSnapshot {queue:{pending:number;running:number;failed:number};workers:{identity:string;last_seen:ISODateTime;state:string}[];modules:{module_id:string;health:string;observed_at:ISODateTime|null;source:string;reason:string|null}[];providers:{id:Id;configured_limit:number|null;observed_active:ObservedMetric;latency_p50:ObservedMetric;latency_p90:ObservedMetric;error_rate:ObservedMetric}[];disk:{mount_id:string;free_bytes:number|null;total_bytes:number|null;application_bytes:number|null;observed_at:ISODateTime|null}[];}
export interface ComparisonCell {paper_version_id:Id;dimension:string;value:string|number|null;unit:string|null;protocol_key:string|null;comparable:boolean;comparability_reason:string|null;source_refs:SourceRef[];missing_reason:MissingReason|null;}
export interface Comparison {paper_version_ids:Id[];selection_hash:string;analysis_revision:string;cells:ComparisonCell[];}
export interface Preferences {theme:'LIGHT'|'DARK'|'SYSTEM';density:'COMFORTABLE'|'COMPACT';reduce_motion:boolean;single_key_shortcuts:boolean;reader_font_px:number;focus_default:boolean;revision:number;}
export interface UiError {error:{code:string;message:string;retryable:boolean;trace_id:Id;details:Record<string,unknown>};}
export interface UiDebug {ui_build:string;contract_version:string;route:string;paper_version_id:Id|null;recent_requests:{method:string;route_template:string;status:number;duration_ms:number;trace_id:Id|null}[];schema_errors:{path:string;expected:string}[];}

export interface Bootstrap {app_version:string;ui_contract_version:string;auth_required:boolean;}
export interface Session {authenticated:boolean;csrf_token:string|null;expires_at:ISODateTime|null;}
export interface VersionList {items:{paper_version_id:Id;version_label:string;document_sha256:string;document_available:boolean}[];}
export interface JobView {job_id:Id;paper_id:Id;paper_version_id:Id;state:TaskState;current_stage:string;tasks_completed:number;tasks_planned:number;eta:{p50_seconds:number|null;p90_seconds:number|null;includes_queue:boolean;sample_count:number;reason:string|null};}
export type JobPage = Page<JobView>;
export interface DeleteResult {deleted:boolean;target_id:Id;}
export interface CollectionWorkspace {collection_id:Id;title:string;selection_hash:string;selected_paper_count:number;analyzed_paper_count:number;analysis_revision:string;insights:{title:string;claim_type:ClaimType;statement:string;source_refs:SourceRef[]}[];}
export interface EntityCard {entity_id:Id;entity_type:string;name:string;aliases:string[];description:FactValue<string>;source_refs:SourceRef[];}
export interface SavedSearch {saved_search_id:Id;name:string;query:LibraryQuery;revision:number;}
export interface DiagnosticExport {asset_id:Id;size_bytes:number;expires_at:ISODateTime;redacted_fields:string[];}

/** Discriminated search result: a claim must never masquerade as a paper. */
export type LibraryResponse =
 | {kind:'PAPERS'; page:Page<PaperListItem>}
 | {kind:'CLAIMS'; page:Page<ReviewItem>}
 | {kind:'TECHNIQUES'; page:Page<EntityCard>};
