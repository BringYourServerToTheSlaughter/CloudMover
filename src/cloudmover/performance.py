"""Read-only JSONL performance report. Run: python -m src.cloudmover.performance LOG --migration-id ID."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path


def summarize(records, migration_id):
    phases=defaultdict(float)
    processes=[]
    upload_processes=[]
    verification_processes=[]
    in_verification=False
    latencies=[]
    files=payload=0
    wall=0.0
    active_start=None
    incomplete=False
    for row in records:
        if row.get('migration_id') != migration_id:
            continue
        event=row.get('event')
        if event=='upload_start':
            files+=row.get('files',0)
            payload+=row.get('logical_bytes',0)
            active_start=row.get('ts')
        elif event=='upload_complete':
            if 'duration_ms' in row:
                wall+=row['duration_ms']/1000
            elif active_start and row.get('ts'):
                wall+=max(0,(datetime.fromisoformat(row['ts'])-datetime.fromisoformat(active_start)).total_seconds())
            active_start=None
        elif event in ('error','user_pause') and active_start:
            incomplete=True
            active_start=None
        elif event=='cli_process':
            processes.append(row)
            if active_start:
                upload_processes.append(row)
            elif in_verification:
                verification_processes.append(row)
        elif event=='verification_start':
            in_verification=True
        elif event=='verification_complete':
            in_verification=False
        elif event=='safe_upload_stage' and row.get('status') in ('completed','error'):
            phase=row.get('phase')
            if phase=='safe_upload_total':
                if row.get('status')=='completed':
                    latencies.append(row['duration_ms']/1000)
            else:
                phases[phase]+=row.get('duration_ms',0)/1000
    incomplete=incomplete or active_start is not None
    def command_summary(rows, *, upload_scope=False):
        grouped=defaultdict(list)
        for row in rows:
            grouped[row['command']].append(row['duration_ms']/1000)
        return {command:{'calls':len(times),'total_seconds':sum(times),
                         'average_seconds':sum(times)/len(times),
                         'percent_upload_wall':sum(times)/wall*100 if upload_scope and wall and not incomplete else None}
                for command,times in sorted(grouped.items())}
    cli_wall=sum(p['duration_ms']/1000 for p in upload_processes)
    upload_cli=sum(p['duration_ms']/1000 for p in upload_processes if p['command']=='filesystem upload')
    stage_total=sum(phases.get(p,0) for p in ('cli_upload','temp_verification','rename_promote','final_verification'))
    # Current production operations are serialized; shared transport is logged once.
    # Old logs lack process telemetry: absence means unknown, never zero launches.
    return {
        'migration_id':migration_id,
        'file_attempts':files,'logical_bytes_attempted':payload,
        'upload_wall_seconds':None if incomplete else wall,
        'upload_cli_seconds':phases.get('cli_upload'),
        'temp_verification_seconds':phases.get('temp_verification'),
        'promote_seconds':phases.get('rename_promote'),
        'final_verification_seconds':phases.get('final_verification'),
        'estimated_outside_stage_seconds':max(0,wall-stage_total) if not incomplete and phases else None,
        'cli_process_count':len(processes) if processes else None,
        'commands_per_file':len(processes)/files if processes and files else None,
        'upload_commands_per_file':len(upload_processes)/files if upload_processes and files else None,
        'commands_all_observed':command_summary(processes),
        'commands_upload_pipeline':command_summary(upload_processes,upload_scope=True),
        'commands_batch_verification':command_summary(verification_processes),
        'upload_command_percent':upload_cli/wall*100 if upload_processes and wall and not incomplete else None,
        'metadata_command_percent':(cli_wall-upload_cli)/wall*100 if upload_processes and wall and not incomplete else None,
        'outside_cli_seconds':max(0,wall-cli_wall) if upload_processes and not incomplete else None,
        'process_startup_seconds':None,  # Not separable from API/crypto/wait time in CLI measurements.
        'api_only_seconds':None,
        'average_cli_seconds':sum(p['duration_ms'] for p in processes)/1000/len(processes) if processes else None,
        'cli_failures':sum(p.get('exit_status')!=0 for p in processes) if processes else None,
        'effective_logical_MB_per_second':payload/1_000_000/wall if wall and not incomplete else None,
        'average_successful_file_latency_seconds':sum(latencies)/len(latencies) if latencies else None,
        'incomplete_upload':incomplete,
    }


def read_records(path):
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row=json.loads(line)
            except ValueError:  # Incomplete final line after a crash.
                continue
            if isinstance(row,dict):
                yield row


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log',type=Path)
    parser.add_argument('--migration-id',required=True)
    args=parser.parse_args()
    print(json.dumps(summarize(read_records(args.log),args.migration_id),indent=2))


if __name__=='__main__':
    main()
