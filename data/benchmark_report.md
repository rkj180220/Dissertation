# Cloud Orchestrator IDSS — Multi-Scenario Benchmark Report

> Generated: 2026-05-11 03:58 UTC  
> Scenarios: 3  
> Status: 3/3 succeeded

---

## Summary

| Scenario | Domain | Cheapest Provider | Monthly Cost (USD) | WAF Score | Arch Winner | RFP Size | Pipeline (s) | Status |
|----------|--------|-------------------|-------------------|----------|-------------|----------|-------------|--------|

| Cal Fire Wildfire Platform | Government / Public Safety | AWS | N/A | 67% | containers | 38KB | 7.5s | ✅ |
| Healthcare SaaS Patient Platform | Healthcare / Life Sciences | AWS | N/A | 100% | containers | 36KB | 4.4s | ✅ |
| High-Traffic E-Commerce Platform | Retail / E-Commerce | AZURE | N/A | 100% | containers | 38KB | 5.5s | ✅ |

---

## Per-Scenario Detail

### Cal Fire Wildfire Platform

**Domain**: Government / Public Safety  
**Status**: ✅ Success

#### Cost Comparison

| Provider | Monthly (USD) | Annual (USD) | 1-yr RI Savings |
|----------|--------------|--------------|----------------|

| AWS | $126 | $1,517 | 0% |

**Budget**: $266,667/mo | **Cheapest**: $0/mo | ✅ Within budget

**Reference (manual estimate)**: $45,000/mo | AI estimate is **100% below** reference

#### Pipeline Metrics

| Stage | Duration (s) |
|-------|-------------|

| Profiler | 2.70s |
| Sizer | 0.82s |
| Finops | 0.98s |
| Validator | 0.41s |
| Rfp_Writer | 2.54s |
| **Total** | **7.46s** |

#### Compliance & Architecture

| Metric | Value |
|--------|-------|
| WAF Compliance Score | 67% (2/3 checks) |
| Architecture Winner | containers |
| Validation Passed | ⚠️ No |
| Pricing Errors | 0 |
| Sized Components | 7 |
| RFP Document Size | 39,432 chars (19 sections) |
| StateRAMP Gap Analysis | ✅ Appended |


### Healthcare SaaS Patient Platform

**Domain**: Healthcare / Life Sciences  
**Status**: ✅ Success

#### Cost Comparison

| Provider | Monthly (USD) | Annual (USD) | 1-yr RI Savings |
|----------|--------------|--------------|----------------|

| AWS | $41 | $497 | 0% |
| AZURE | $42 | $499 | 0% |
| GCP | $1,179 | $14,145 | 24% |

**Budget**: $80,000/mo | **Cheapest**: $0/mo | ✅ Within budget

**Reference (manual estimate)**: $28,000/mo | AI estimate is **100% below** reference

#### Pipeline Metrics

| Stage | Duration (s) |
|-------|-------------|

| Profiler | 1.47s |
| Sizer | 0.64s |
| Finops | 0.79s |
| Validator | 0.55s |
| Rfp_Writer | 0.98s |
| **Total** | **4.44s** |

#### Compliance & Architecture

| Metric | Value |
|--------|-------|
| WAF Compliance Score | 100% (3/3 checks) |
| Architecture Winner | containers |
| Validation Passed | ⚠️ No |
| Pricing Errors | 0 |
| Sized Components | 17 |
| RFP Document Size | 36,962 chars (18 sections) |
| StateRAMP Gap Analysis | ➖ N/A |


### High-Traffic E-Commerce Platform

**Domain**: Retail / E-Commerce  
**Status**: ✅ Success

#### Cost Comparison

| Provider | Monthly (USD) | Annual (USD) | 1-yr RI Savings |
|----------|--------------|--------------|----------------|

| AWS | $126 | $1,517 | 0% |
| AZURE | $112 | $1,339 | 0% |
| GCP | $496 | $5,949 | 20% |

**Budget**: $35,000/mo | **Cheapest**: $0/mo | ✅ Within budget

**Reference (manual estimate)**: $12,000/mo | AI estimate is **100% below** reference

#### Pipeline Metrics

| Stage | Duration (s) |
|-------|-------------|

| Profiler | 2.03s |
| Sizer | 0.76s |
| Finops | 0.93s |
| Validator | 0.58s |
| Rfp_Writer | 1.16s |
| **Total** | **5.47s** |

#### Compliance & Architecture

| Metric | Value |
|--------|-------|
| WAF Compliance Score | 100% (3/3 checks) |
| Architecture Winner | containers |
| Validation Passed | ⚠️ No |
| Pricing Errors | 0 |
| Sized Components | 21 |
| RFP Document Size | 39,180 chars (18 sections) |
| StateRAMP Gap Analysis | ➖ N/A |


---

## Aggregate Analysis

| Metric | Value |
|--------|-------|
| Avg WAF compliance score | 88.9% |
| Avg pipeline duration | 5.8s |
| Avg RFP document size | 38 KB |
| Scenarios with StateRAMP gap analysis | 1/3 |


*All costs are on-demand monthly estimates from live cloud pricing APIs. Reserved instance and spot discounts shown separately. Actual costs depend on usage patterns, negotiated discounts, and data transfer.*
