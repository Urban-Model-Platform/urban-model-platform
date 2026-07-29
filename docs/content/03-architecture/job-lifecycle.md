# Job Lifecycle and State Transitions

This document provides visual models of the job handling flow in the Urban Model Platform.

## Job State Machine

```mermaid
stateDiagram-v2
    [*] --> accepted: POST /processes/{id}/execution
    
    accepted --> running: Remote provider starts processing
    accepted --> successful: Immediate results (sync execution)
    accepted --> failed: Forward error / Invalid request
    
    running --> running: Polling continues
    running --> successful: Processing complete
    running --> failed: Processing error / Timeout
    
    successful --> [*]
    failed --> [*]
    
    note right of accepted
        Initial state after job creation
        Location header returned
        Background polling scheduled
    end note
    
    note right of running
        Polling active
        Status updates via observers
        Timeout monitoring
    end note
    
    note right of successful
        Results available
        Results verification triggered
        Polling stopped
    end note
    
    note right of failed
        Terminal state
        No further processing
        Diagnostic info available
    end note
```

## Job Creation and Forwarding Flow

```mermaid
flowchart TD
    Start([POST /processes/id/execution]) --> Validate[Validate Process ID]
    Validate --> CreateJob[Create Local Job UUID]
    CreateJob --> PersistAccepted[Persist Accepted Status]
    PersistAccepted --> NotifyCreated[Notify Observers: on_job_created]
    NotifyCreated --> RecordHistory1[Observer: Record Status History]
    
    RecordHistory1 --> Forward[Forward to Remote Provider]
    Forward --> SafeForward{Retry Logic}
    
    SafeForward -->|Transient Error| Retry[Exponential Backoff Retry]
    Retry -->|Max Retries| MarkFailed1[Mark Job Failed]
    Retry -->|Success| CheckResponse
    SafeForward -->|4xx Error| MarkFailed1
    SafeForward -->|Success| CheckResponse[Check Response Status]
    
    CheckResponse -->|≥400 Non-StatusInfo| PropagateError[Propagate Upstream Error]
    CheckResponse -->|Valid Response| DeriveStatus[Derive Status via Strategy Pattern]
    
    DeriveStatus --> StrategySelect{Response Pattern?}
    StrategySelect -->|StatusInfo in Body| DirectStrategy[DirectStatusInfoStrategy]
    StrategySelect -->|Outputs No Status| ImmediateStrategy[ImmediateResultsStrategy]
    StrategySelect -->|Location Header| LocationStrategy[LocationFollowupStrategy]
    StrategySelect -->|Unparseable| FallbackStrategy[FallbackFailedStrategy]
    
    DirectStrategy --> Normalize[Normalize & Enrich Status]
    ImmediateStrategy --> Normalize
    LocationStrategy --> Normalize
    FallbackStrategy --> Normalize
    
    Normalize --> CheckImmediate{Immediate Success?}
    CheckImmediate -->|Yes| VerifyResults[Verify Remote Results]
    CheckImmediate -->|No| Finalize
    
    VerifyResults -->|Failed| Downgrade[Downgrade to Failed]
    VerifyResults -->|Success| Finalize[Finalize Job]
    Downgrade --> Finalize
    
    Finalize --> UpdateJob[Update Job State]
    UpdateJob --> NotifyChanged[Notify Observers: on_status_changed]
    NotifyChanged --> RecordHistory2[Observer: Record Status]
    NotifyChanged --> CheckPolling{Remote URL & Not Terminal?}
    
    CheckPolling -->|Yes| SchedulePoll[Observer: Schedule Polling]
    CheckPolling -->|No| CheckTerminal{Terminal State?}
    
    CheckTerminal -->|Yes| NotifyComplete[Notify Observers: on_job_completed]
    CheckTerminal -->|No| Return
    
    NotifyComplete --> VerifyObs[Observer: Verify Results]
    VerifyObs --> Return[Return 201 + Accepted Status]
    SchedulePoll --> Return
    
    MarkFailed1 --> Return
    PropagateError --> Return
    
    Return --> End([Client Polls GET /jobs/id])
    
    style Start fill:#e1f5ff
    style End fill:#e1f5ff
    style DeriveStatus fill:#fff4e6
    style StrategySelect fill:#fff4e6
    style NotifyCreated fill:#f3e5f5
    style NotifyChanged fill:#f3e5f5
    style NotifyComplete fill:#f3e5f5
    style RecordHistory1 fill:#e8f5e9
    style RecordHistory2 fill:#e8f5e9
    style SchedulePoll fill:#e8f5e9
    style VerifyObs fill:#e8f5e9
```

## Polling Loop Flow

```mermaid
flowchart TD
    Start([Poll Task Started]) --> WhileLoop{Shutdown?}
    
    WhileLoop -->|Yes| Cleanup[Cancel All Polls]
    WhileLoop -->|No| CheckStop[Check Termination Conditions]
    
    CheckStop --> GetJob[Get Fresh Job State]
    GetJob --> Conditions{Should Stop?}
    
    Conditions -->|Job Not Found| Stop1[Log: Job Disappeared]
    Conditions -->|Terminal State| Stop2[Log: Terminal Reached]
    Conditions -->|No Remote URL| Stop3[Log: No Remote URL]
    Conditions -->|Timeout Exceeded| HandleTimeout[Handle Timeout]
    Conditions -->|Continue| PollRemote[Poll Remote Status URL]
    
    HandleTimeout --> MarkTimeout[Mark Job Failed]
    MarkTimeout --> NotifyTimeout[Notify Observers: Timeout]
    NotifyTimeout --> Stop4[Stop Polling]
    
    PollRemote --> FetchStatus{HTTP GET}
    FetchStatus -->|Error| LogError[Log Error & Continue]
    FetchStatus -->|Success| ExtractStatus[Extract StatusInfo]
    
    ExtractStatus -->|Invalid| LogSkip[Log: No Valid Status]
    ExtractStatus -->|Valid| ProcessUpdate[Process Status Update]
    
    ProcessUpdate --> NormalizeID[Normalize Remote Job ID]
    NormalizeID --> CheckEnrich{Needs Enrichment?}
    
    CheckEnrich -->|Yes| EnrichFields[Enrich started/progress/message]
    CheckEnrich -->|No| UpdateTimestamp
    EnrichFields --> UpdateTimestamp[Update Timestamp]
    
    UpdateTimestamp --> AddLinks[Ensure Self & Results Links]
    AddLinks --> ApplyStatus[Apply Status to Job]
    ApplyStatus --> PersistUpdate[Persist to Repository]
    PersistUpdate --> NotifyObservers[Notify Observers: on_status_changed]
    
    NotifyObservers --> RecordHistory[Observer: Record Status]
    NotifyObservers --> CheckTerminal{Terminal State?}
    
    CheckTerminal -->|Yes| NotifyComplete[Notify: on_job_completed]
    CheckTerminal -->|No| Sleep
    
    NotifyComplete --> VerifyResults[Observer: Verify Results]
    VerifyResults --> StopPolling[Stop Polling]
    
    LogError --> Sleep[Sleep Poll Interval]
    LogSkip --> Sleep
    Sleep --> WhileLoop
    
    Stop1 --> End([Polling Ended])
    Stop2 --> End
    Stop3 --> End
    Stop4 --> End
    StopPolling --> End
    Cleanup --> End
    
    style Start fill:#e1f5ff
    style End fill:#e1f5ff
    style ProcessUpdate fill:#fff4e6
    style NotifyObservers fill:#f3e5f5
    style NotifyComplete fill:#f3e5f5
    style NotifyTimeout fill:#f3e5f5
    style RecordHistory fill:#e8f5e9
    style VerifyResults fill:#e8f5e9
```

## Observer Pattern Interactions

```mermaid
sequenceDiagram
    participant JM as JobManager
    participant SH as StatusHistoryObserver
    participant PS as PollingSchedulerObserver
    participant RV as ResultsVerificationObserver
    participant Repo as JobRepository
    
    Note over JM: Job Created
    JM->>+SH: on_job_created(job, accepted_status)
    SH->>Repo: append_status(job.id, accepted_status)
    SH-->>-JM: ✓
    
    Note over JM: Status Changed to Running
    JM->>+SH: on_status_changed(job, old, running_status)
    SH->>Repo: append_status(job.id, running_status)
    SH-->>-JM: ✓
    
    JM->>+PS: on_status_changed(job, old, running_status)
    Note over PS: Check if should poll
    PS->>PS: job.remote_status_url && !terminal?
    PS->>JM: _schedule_poll(job.id)
    Note over JM: Background poll task created
    PS-->>-JM: ✓
    
    JM->>+RV: on_status_changed(job, old, running_status)
    Note over RV: No action for non-terminal
    RV-->>-JM: ✓
    
    Note over JM: Poll Loop Running
    Note over JM: ...
    Note over JM: Status Changed to Successful
    
    JM->>+SH: on_status_changed(job, running, success_status)
    SH->>Repo: append_status(job.id, success_status)
    SH-->>-JM: ✓
    
    JM->>+PS: on_status_changed(job, running, success_status)
    Note over PS: Terminal state, no polling
    PS-->>-JM: ✓
    
    JM->>+RV: on_status_changed(job, running, success_status)
    Note over RV: No action until completed
    RV-->>-JM: ✓
    
    Note over JM: Job Completed
    JM->>+SH: on_job_completed(job, success_status)
    Note over SH: Already recorded in status_changed
    SH-->>-JM: ✓
    
    JM->>+PS: on_job_completed(job, success_status)
    Note over PS: Terminal, no action
    PS-->>-JM: ✓
    
    JM->>+RV: on_job_completed(job, success_status)
    Note over RV: Extract results URL from links
    RV->>RV: GET results_url (verify accessible)
    Note over RV: Log warning if inaccessible
    RV-->>-JM: ✓
```

## Strategy Pattern: Status Derivation

```mermaid
flowchart TD
    Start([Provider Response Received]) --> Context[Create StatusDerivationContext]
    Context --> Orchestrator[StatusDerivationOrchestrator]
    
    Orchestrator --> CheckDirect{DirectStatusInfoStrategy.can_handle?}
    CheckDirect -->|Yes| DirectDerive[DirectStatusInfoStrategy.derive]
    CheckDirect -->|No| CheckImmediate{ImmediateResultsStrategy.can_handle?}
    
    CheckImmediate -->|Yes| ImmediateDerive[ImmediateResultsStrategy.derive]
    CheckImmediate -->|No| CheckLocation{LocationFollowupStrategy.can_handle?}
    
    CheckLocation -->|Yes| LocationDerive[LocationFollowupStrategy.derive]
    CheckLocation -->|No| FallbackDerive[FallbackFailedStrategy.derive]
    
    DirectDerive --> DirectCheck{StatusInfo in Body?}
    DirectCheck -->|Yes| ExtractDirect[Extract StatusInfo]
    DirectCheck -->|No| DirectFailed[Return Failed Status]
    ExtractDirect --> CaptureRemote[Capture Remote Job ID]
    CaptureRemote --> CheckLocationHeader{Location Header?}
    CheckLocationHeader -->|Yes| StoreURL[Store Remote Status URL]
    CheckLocationHeader -->|No| Result
    
    ImmediateDerive --> SynthesizeSuccess[Synthesize Successful Status]
    SynthesizeSuccess --> InjectOutputs[Inject Outputs into Status]
    InjectOutputs --> Result
    
    LocationDerive --> FollowLocation[GET Location URL]
    FollowLocation -->|Success| ExtractFollowed[Extract StatusInfo]
    FollowLocation -->|Error| LocationFailed[Return Failed Status]
    ExtractFollowed --> Result
    LocationFailed --> Result
    
    FallbackDerive --> CreateFailed[Create Failed StatusInfo]
    CreateFailed --> SetDiagnostic[Set Diagnostic Message]
    SetDiagnostic --> Result
    
    StoreURL --> Result[Return StatusDerivationResult]
    DirectFailed --> Result
    Result --> End([StatusInfo + Remote URLs])
    
    style Start fill:#e1f5ff
    style End fill:#e1f5ff
    style Orchestrator fill:#fff4e6
    style DirectDerive fill:#e8f5e9
    style ImmediateDerive fill:#e8f5e9
    style LocationDerive fill:#e8f5e9
    style FallbackDerive fill:#ffebee
```

## Retry Logic Flow

```mermaid
flowchart TD
    Start([Forward Request to Provider]) --> Execute[_safe_forward]
    
    Execute --> Classify[do_forward_with_error_classification]
    Classify --> Post[HTTP POST to exec_url]
    
    Post -->|Success| Return[Return Response]
    Post -->|Exception| CheckException{Exception Type?}
    
    CheckException -->|OGCProcessException| CheckTransient{_is_transient_error?}
    CheckException -->|Other| PropagateOther[Re-raise]
    
    CheckTransient -->|502/503/504| WrapTransient[Wrap in TransientOGCError]
    CheckTransient -->|4xx| PropagateNonTransient[Re-raise as-is]
    
    WrapTransient --> RetryAdapter{Retry Adapter Available?}
    
    RetryAdapter -->|Yes| Retry[TenacityRetryAdapter.execute]
    RetryAdapter -->|No| SingleAttempt[Single Attempt Only]
    
    Retry --> Attempt1[Attempt 1]
    Attempt1 -->|TransientOGCError| Wait1[Wait base_wait seconds]
    Attempt1 -->|Success| ReturnSuccess[Return Response]
    
    Wait1 --> Attempt2[Attempt 2]
    Attempt2 -->|TransientOGCError| Wait2[Wait base_wait * 2 seconds]
    Attempt2 -->|Success| ReturnSuccess
    
    Wait2 --> Attempt3[Attempt 3]
    Attempt3 -->|TransientOGCError| ExhaustedRetry[Retry Exhausted]
    Attempt3 -->|Success| ReturnSuccess
    
    ExhaustedRetry --> UnwrapError[Unwrap TransientOGCError]
    UnwrapError --> MarkFailed[Mark Job Failed]
    
    SingleAttempt -->|TransientOGCError| MarkFailed
    PropagateNonTransient --> MarkFailed
    PropagateOther --> MarkFailed
    
    MarkFailed --> LogError[Log Error]
    LogError --> ReturnNone[Return None]
    
    Return --> End([Continue Processing])
    ReturnSuccess --> End
    ReturnNone --> End
    
    style Start fill:#e1f5ff
    style End fill:#e1f5ff
    style RetryAdapter fill:#fff4e6
    style Retry fill:#e8f5e9
    style MarkFailed fill:#ffebee
```

## Key Design Patterns Summary

| Pattern | Location | Purpose |
|---------|----------|---------|
| **Strategy** | Status Derivation | Handle different provider response formats |
| **Observer** | State Transitions | Decouple side effects from core logic |
| **Adapter** | Retry Logic | Abstract retry implementation (Tenacity) |
| **Port/Adapter** | All External Deps | Hexagonal architecture boundaries |
| **Guard Clause** | Polling Loop | Reduce nesting, early returns |
| **Command Query Separation** | Helper Methods | Queries return values, commands perform actions |

## State Transition Rules

### From `accepted`
- ✅ → `running` (remote provider starts processing)
- ✅ → `successful` (immediate sync results)
- ✅ → `failed` (forward error, invalid request)

### From `running`
- ✅ → `running` (polling continues, no change)
- ✅ → `successful` (processing complete)
- ✅ → `failed` (processing error, timeout)

### From `successful`
- ❌ Terminal state, no transitions

### From `failed`
- ❌ Terminal state, no transitions

## Error Handling Strategy

```mermaid
flowchart LR
    Error([Error Occurred]) --> Classify{Error Type}
    
    Classify -->|Connection/Timeout| Transient[Transient Error]
    Classify -->|502/503/504| Transient
    Classify -->|4xx Client Error| NonTransient[Non-Transient Error]
    Classify -->|Auth Error| NonTransient
    
    Transient --> Retry[Retry with Backoff]
    Retry -->|Success| Success[Continue]
    Retry -->|Max Attempts| Failed[Mark Job Failed]
    
    NonTransient --> FailImmediate[Fail Immediately]
    FailImmediate --> Failed
    
    Failed --> Diagnostic[Store Diagnostic Info]
    Diagnostic --> NotifyObservers[Notify Observers]
    NotifyObservers --> End([Return Error Response])
    
    Success --> End
    
    style Error fill:#ffebee
    style Transient fill:#fff9c4
    style NonTransient fill:#ffccbc
    style Success fill:#c8e6c9
    style Failed fill:#ffebee
```

## Configuration Impact

| Setting | Default | Impact |
|---------|---------|--------|
| `poll_interval` | 5.0s | Frequency of status checks (global default, can be overridden per-process) |
| `ProcessConfig.ttw_job_done` | None | Max time before timeout (per-process override, optional) |
| `ProviderConfig.ttw_job_done` | 300s | Max time before timeout (provider-wide default) |
| `forward_max_retries` | 3 | Retry attempts for transient errors |
| `forward_retry_base_wait` | 1.0s | Initial wait between retries |
| `forward_retry_max_wait` | 5.0s | Maximum wait between retries |
| `rewrite_remote_links` | True | Replace remote URLs with local |
| `inline_inputs_size_limit` | 64KB | Max size for inline input storage |

### Timeout Resolution Hierarchy

When a job exceeds the time-to-wait threshold, `JobManager` marks it as failed. The timeout is resolved in this order:

1. **ProcessConfig.ttw_job_done** — If set on a specific process in `providers.yaml`, use this (most specific)
2. **ProviderConfig.ttw_job_done** — Fall back to provider-level default (300s)
3. **No timeout** — If neither is set, job can run indefinitely (not recommended for production)
