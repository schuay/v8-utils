// Workload for the samply fixture profile: one hot function that tiers up to
// Turbofan, a regexp, and a Maglev-tier closure.
function hotLoop(n) {
  let s = 0;
  for (let i = 0; i < n; i++) s = (s + i * i) % 1000003;
  return s;
}

const re = /(a+)+b/;
function matchSome(k) {
  let hits = 0;
  for (let i = 0; i < k; i++) hits += re.test("aaaaaaaaaaaaaaab") ? 1 : 0;
  return hits;
}

let t = 0;
const end = Date.now() + 400;
while (Date.now() < end) t += hotLoop(20000) + matchSome(50);
print(t);
