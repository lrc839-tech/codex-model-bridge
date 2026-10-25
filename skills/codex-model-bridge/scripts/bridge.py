#!/usr/bin/env python3
"""Portable skill entry point. Standard library only; Python 3.9+."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from runtime import config, engine
from runtime.providers import ProviderError


def main():
    parser=argparse.ArgumentParser(description='桌面 Codex 主控、手动选择的外部模型执行器')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    setup=sub.add_parser('configure')
    setup.add_argument('--port',type=int,default=0)
    setup.add_argument('--no-browser',action='store_true')
    sub.add_parser('probe')
    run=sub.add_parser('run')
    run.add_argument('--task-file',required=True)
    run.add_argument('--mixed',action='store_true')
    run.add_argument('--dry-run',action='store_true')
    run.add_argument('--max-repairs',type=int,choices=[0,1,2],default=2)
    run.add_argument('--timeout',type=int,default=300)
    run.add_argument('--details',action='store_true')
    apply=sub.add_parser('apply')
    apply.add_argument('--run-dir',required=True)
    apply.add_argument('--desktop-reviewed',action='store_true')
    apply.add_argument('--review-file',help='Codex 逐项填写的验收证据 JSON，默认 run-dir/desktop-review.json')
    measure_start=sub.add_parser('measure-start',help='记录当前 Codex 会话的任务起点，不调用模型')
    measure_start.add_argument('--output',required=True)
    measure_start.add_argument('--label')
    measure_start.add_argument('--boundary',choices=['before_current_turn','now'],default='before_current_turn')
    measure_start.add_argument('--session-id',help='协调器显式指定候选会话；默认当前环境会话 ID')
    measure_end=sub.add_parser('measure-end',help='从同一会话的数字记录计算用量窗口')
    measure_end.add_argument('--start-file',required=True)
    measure_end.add_argument('--output',required=True)
    measure_end.add_argument('--session-id')
    args=parser.parse_args()
    try:
        if args.command=='configure':
            from runtime.server import serve
            serve(args.port,not args.no_browser)
            return 0
        elif args.command=='status':
            result={**config.public_profile(),'automatic_delegation':False,'desktop_planning':True,
                    'desktop_acceptance':True,'hybrid_workflow':'saver','external_inputs':['text'],
                    'external_image_transport':False}
            try:
                result['codex_cli']=engine.codex_binary()
            except ValueError:
                result['codex_cli']=None
        elif args.command=='probe':
            from runtime.server import test_connection
            result=test_connection(config.load_profile())
        elif args.command in {'measure-start','measure-end'}:
            from runtime import measurements
            if args.command=='measure-start':
                result=measurements.mark_start(args.label,boundary=args.boundary,session_id=args.session_id)
            else:
                result=measurements.mark_end(json.loads(Path(args.start_file).read_text()),session_id=args.session_id)
            engine.save_json(Path(args.output).expanduser().absolute(),result)
        elif args.command=='run':
            config.ensure_state()
            # Dry run validates file contracts without credentials or any paid API call.
            profile={} if args.dry_run else config.load_profile()
            args.model=profile.get('model','dry-run')
            args.review_model=args.model
            result=engine.run_task(args)
        elif args.command=='apply':
            result=engine.apply_run(args)
        display=engine.compact_handoff(result) if args.command=='run' and not args.details else result
        print(json.dumps(display,ensure_ascii=False,indent=2))
        return 1 if result.get('status') in {'failed','needs_codex_attention','integrated_needs_codex_attention'} or result.get('ok') is False else 0
    except (ValueError,OSError,KeyError,TypeError,ProviderError) as error:
        # Safe errors from provider adapter never contain upstream bodies or keys.
        print(json.dumps({'status':'error','error':str(error)},ensure_ascii=False))
        return 2


if __name__=='__main__':
    sys.exit(main())
