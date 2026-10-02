import httpx
import asyncio

async def test():
    async with httpx.AsyncClient() as client:
        try:
            # Testing with a non-existent port to force a connection error
            response = await client.post("http://localhost:12345/api/generate", json={"model": "test", "prompt": "hi"})
            response.raise_for_status()
        except Exception as e:
            print(f"Error: {e}")
            print(f"Error string: {str(e)}")
            import traceback
            traceback.print_exc()

asyncio.run(test())
