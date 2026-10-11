import asyncio, os
import uvicorn
from bot import bot, main as run_bot

async def serve_dashboard():
    config=uvicorn.Config('dashboard:app',host='0.0.0.0',port=int(os.getenv('PORT','8080')),log_level='info',proxy_headers=True,forwarded_allow_ips='*')
    server=uvicorn.Server(config)
    await server.serve()

async def main():
    tasks=[asyncio.create_task(run_bot()), asyncio.create_task(serve_dashboard())]
    done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_EXCEPTION)
    for task in pending: task.cancel()
    for task in done:
        exc=task.exception()
        if exc: raise exc

if __name__=='__main__':
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
