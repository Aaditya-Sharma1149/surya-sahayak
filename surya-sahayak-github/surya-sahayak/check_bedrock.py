"""Find which Bedrock model IDs work from this machine/region.

    python check_bedrock.py
Prints the export lines to paste into your shell / .env.
"""
import os

import boto3

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-southeast-2"
VISION = ["apac.amazon.nova-lite-v1:0", "amazon.nova-lite-v1:0", "global.amazon.nova-2-lite-v1:0",
          "apac.amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0"]
AGENT = ["apac.amazon.nova-pro-v1:0", "amazon.nova-pro-v1:0", "global.amazon.nova-2-lite-v1:0",
         "apac.amazon.nova-lite-v1:0", "amazon.nova-lite-v1:0"]

c = boto3.client("bedrock-runtime", region_name=REGION)
print(f"Region: {REGION}")
try:
    print("Identity:", boto3.client("sts").get_caller_identity()["Arn"])
except Exception as e:
    print("!! No AWS credentials:", e)
    raise SystemExit(1)

ok = {}
for mid in dict.fromkeys(VISION + AGENT):
    try:
        r = c.converse(modelId=mid, messages=[{"role": "user", "content": [{"text": "Reply with OK"}]}],
                       inferenceConfig={"maxTokens": 5})
        ok[mid] = True
        print(f"  OK    {mid}")
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
        print(f"  FAIL  {mid}  ({code})")

bill = next((m for m in VISION if ok.get(m)), None)
agent = next((m for m in AGENT if ok.get(m)), None)
print()
if bill and agent:
    print("Paste these:")
    print(f"export AWS_REGION={REGION}")
    print(f"export BILL_MODEL_ID={bill}")
    print(f"export AGENT_MODEL_ID={agent}")
else:
    print("No model works yet. Bedrock console -> Model access -> enable Amazon Nova Lite and Nova Pro,")
    print("and make sure the EC2 IAM role has bedrock:InvokeModel and bedrock:Converse.")
