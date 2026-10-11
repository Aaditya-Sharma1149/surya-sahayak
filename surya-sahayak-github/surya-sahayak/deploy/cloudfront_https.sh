#!/bin/bash
# Surya Sahayak: free HTTPS address for the EC2 app, so the browser will offer GPS.
#
# Run this in AWS CloudShell (the >_ icon at the top of the AWS console). It uses your console login,
# so nothing has to be installed or configured. It creates a CloudFront distribution that serves
# https://dxxxxxxxx.cloudfront.net and forwards everything to your EC2 server on port 80.
#
# Browsers only allow location access on https pages (or localhost), which is why this is needed.
set -u

INSTANCE_ID="${INSTANCE_ID:-}"          # e.g.  INSTANCE_ID=i-0123456789abcdef0 bash cloudfront_https.sh
if [ -z "$INSTANCE_ID" ] && [ -z "${DNS:-}" ]; then
  echo "Tell the script which server to use, for example:"
  echo "   INSTANCE_ID=i-0123456789abcdef0 bash cloudfront_https.sh"
  echo "or DNS=ec2-1-2-3-4.ap-south-1.compute.amazonaws.com bash cloudfront_https.sh"
  exit 1
fi

echo "== Finding your server"
DNS="${DNS:-}"
if [ -z "$DNS" ]; then
  for R in ${AWS_REGION:-} ap-southeast-2 us-east-1 ap-south-1 us-west-2 us-east-2 eu-west-1 eu-central-1 ap-southeast-1 ap-northeast-1; do
    OUT=$(aws ec2 describe-instances --region "$R" --instance-ids "$INSTANCE_ID" \
          --query 'Reservations[0].Instances[0].PublicDnsName' --output text 2>/dev/null)
    if [ -n "$OUT" ] && [ "$OUT" != "None" ]; then DNS="$OUT"; echo "   found in $R: $DNS"; break; fi
  done
fi
if [ -z "$DNS" ]; then
  echo "Could not find the server automatically."
  echo "Open EC2 > Instances > click your instance > copy 'Public IPv4 DNS', then run:"
  echo "   DNS=<paste it here> bash cloudfront_https.sh"
  exit 1
fi

echo "== Looking up CloudFront's standard 'no caching' and 'forward everything' policies"
CACHE_ID=$(aws cloudfront list-cache-policies --type managed \
  --query "CachePolicyList.Items[?CachePolicy.CachePolicyConfig.Name=='Managed-CachingDisabled'].CachePolicy.Id | [0]" --output text 2>/dev/null)
ORIGIN_ID=$(aws cloudfront list-origin-request-policies --type managed \
  --query "OriginRequestPolicyList.Items[?OriginRequestPolicy.OriginRequestPolicyConfig.Name=='Managed-AllViewer'].OriginRequestPolicy.Id | [0]" --output text 2>/dev/null)
[ -z "$CACHE_ID" ] || [ "$CACHE_ID" = "None" ] && CACHE_ID="4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
[ -z "$ORIGIN_ID" ] || [ "$ORIGIN_ID" = "None" ] && ORIGIN_ID="216adef6-5c7f-47e4-b989-5492eafa07d3"
echo "   cache policy $CACHE_ID, origin request policy $ORIGIN_ID"

cat > cf.json <<EOF
{
  "CallerReference": "surya-sahayak-$(date +%s)",
  "Comment": "Surya Sahayak: HTTPS front door for the EC2 app",
  "Enabled": true,
  "HttpVersion": "http2",
  "PriceClass": "PriceClass_All",
  "Origins": {
    "Quantity": 1,
    "Items": [{
      "Id": "surya-ec2",
      "DomainName": "$DNS",
      "CustomOriginConfig": {
        "HTTPPort": 80,
        "HTTPSPort": 443,
        "OriginProtocolPolicy": "http-only",
        "OriginReadTimeout": 60,
        "OriginKeepaliveTimeout": 5
      }
    }]
  },
  "DefaultCacheBehavior": {
    "TargetOriginId": "surya-ec2",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {
      "Quantity": 7,
      "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
      "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}
    },
    "Compress": true,
    "CachePolicyId": "$CACHE_ID",
    "OriginRequestPolicyId": "$ORIGIN_ID"
  },
  "ViewerCertificate": {"CloudFrontDefaultCertificate": true}
}
EOF

echo "== Creating the HTTPS address (this part takes a few seconds)"
if ! RESULT=$(aws cloudfront create-distribution --distribution-config file://cf.json \
      --query 'Distribution.[Id,DomainName]' --output text 2>&1); then
  echo "CloudFront said no:"; echo "$RESULT"
  echo "If it mentions AccessDenied, your console user cannot create CloudFront distributions; use the root or an admin user."
  exit 1
fi
DIST_ID=$(echo "$RESULT" | awk '{print $1}')
DOMAIN=$(echo "$RESULT" | awk '{print $2}')
echo "   created $DIST_ID -> https://$DOMAIN"

echo "== Waiting for it to go live (usually 3 to 8 minutes; leave this window open)"
aws cloudfront wait distribution-deployed --id "$DIST_ID" && \
  echo && echo "READY. Open this on your phone or laptop:  https://$DOMAIN/" && echo
