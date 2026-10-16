            await ws.send(json.dumps({"type":"subscribe","channel":f"order_book/{market_id}"}))
            while time.monotonic() - begin < seconds:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    if last_update is not None:
                        last_event_ages.append(time.time() - last_update)
                    continue
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await ws.send(json.dumps({"type":"pong"}))
                    continue
                if msg.get("type") not in ("subscribed/order_book", "update/order_book"):
                    continue
                channel = str(msg.get("channel", ""))
                if channel not in (f"order_book:{market_id}", f"order_book/{market_id}"):
                    continue
                messages += 1
                if msg["type"] == "update/order_book":
                    updates += 1
                ob = msg.get("order_book") or {}
                stamp = ob.get("last_updated_at", msg.get("last_updated_at"))
                divisor = 1e6
                if stamp is None:
                    stamp = msg.get("timestamp")
                    divisor = 1e3
                if stamp is None:
                    missing += 1
                    continue
                try:
                    event_time = float(stamp) / divisor
                    age = time.time() - event_time
                    if not math.isfinite(age) or age < -2 or age > 86400:
                        invalid += 1
                        continue
                except (TypeError, ValueError, OverflowError):
                    invalid += 1
                    continue
                event_ages.append(age)
                last_event_ages.append(age)
                if last_update is not None:
                    gaps.append(time.time() - last_update)
                last_update = time.time()
                latest_source_ts = event_time
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
    def stats(data):
        if not data:
            return None
        data = sorted(data)
        return {"count":len(data),"p50":round(statistics.median(data),3),
                "p95":round(data[min(len(data)-1, math.ceil(.95*len(data))-1)],3),
                "p99":round(data[min(len(data)-1, math.ceil(.99*len(data))-1)],3),
                "max":round(data[-1],3)}
    return {"messages":messages,"updates":updates,"missing":missing,"invalid":invalid,
            "event_age_sec":stats(event_ages),"receive_gap_sec":stats(gaps),
            "observed_quote_age_sec":stats(last_event_ages),
            "sampled_quote_age_sec":stats(sampled_quote_ages),
            "no_book_samples":no_book_samples,
            "hypothetical_reject_fraction":{str(t):round(sum(a>t or a < -2 for a in sampled_quote_ages)/len(sampled_quote_ages),4) if sampled_quote_ages else None for t in (2,5,10,30,60)}}

async def main():
    try:
        market, endpoint = await asyncio.to_thread(resolve_market)
        print("P0-02 RH MARKET",json.dumps({"symbol":"SNDK","market_id":market,"metadata_endpoint":endpoint}))
        result = await observe(market)
        result.update({"symbol":"SNDK","market_id":market})
        print("P0-02 RH RESULT",json.dumps(result,sort_keys=True))
        if result["messages"] == 0 or not result["event_age_sec"]:
            raise RuntimeError("No valid RH SNDK timestamp observations")
    except Exception as exc:
        print("P0-02 RH INCONCLUSIVE",json.dumps({"error":f"{type(exc).__name__}: {exc}"}))
        raise

if __name__ == "__main__":
    asyncio.run(main())
